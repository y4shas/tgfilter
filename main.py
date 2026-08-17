import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

from telethon import TelegramClient, events

from config import (
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    TELEGRAM_GROUP_ID,
    TELEGRAM_SESSION_NAME,
    DOWNLOAD_DIR,
    BATCH_SIZE,
    BATCH_INTERVAL_SECONDS,
    validate_config,
)
from gemini_analyzer import analyze_batch, build_attachment_parts, DEFAULT_RESULT
from discord_notifier import send_notification

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
     handlers=[
        logging.FileHandler("app.log", mode="a"),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("job_watcher")

client = TelegramClient(TELEGRAM_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)

# Buffer of messages awaiting the next Gemini batch call.
# Each entry: {"id", "chat_id", "text", "parts", "posted_at"}
pending = []
pending_lock = asyncio.Lock()
flush_now = asyncio.Event()


async def download_attachments(message):
    """Download any media on the message. Returns list of {"path", "filename"}."""
    attachments = []
    if message.media:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        try:
            path = await message.download_media(file=DOWNLOAD_DIR)
            if path:
                attachments.append({"path": path, "filename": os.path.basename(path)})
        except Exception as e:
            logger.error(f"Failed to download media for message {message.id}: {e}")
    return attachments


def build_source_link(chat_id, message_id):
    """Build a t.me deep link back to the original message (works for supergroups)."""
    cid = str(chat_id)
    cid = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    return f"https://t.me/c/{cid}/{message_id}"


def is_service_message(message) -> bool:
    """True for Telegram service messages (user joined/left, pinned message, group photo
    changed, etc.) rather than an actual chat message from a member."""
    return getattr(message, "action", None) is not None


async def build_entry_from_message(message, chat_id):
    """Download/extract any attachment and build the buffer entry for one Telegram message.
    Shared by the live handler and the backfill test path."""
    text = message.message or ""

    attachments = await download_attachments(message)
    try:
        parts = await asyncio.to_thread(build_attachment_parts, attachments)
    finally:
        for att in attachments:
            try:
                os.remove(att["path"])
            except OSError:
                pass

    return {
        "id": str(message.id),
        "chat_id": chat_id,
        "text": text,
        "parts": parts,
        "posted_at": message.date or datetime.now(timezone.utc),
    }


@client.on(events.NewMessage(chats=TELEGRAM_GROUP_ID))
async def handle_new_message(event):
    """On arrival: download + extract/upload any attachment right away (so local files don't
    need to stick around until batch flush time), then buffer the message for the next batch."""
    message = event.message

    if is_service_message(message):
        logger.debug(f"Ignoring service message {message.id} (join/leave/pin/etc.).")
        return

    entry = await build_entry_from_message(message, event.chat_id)

    async with pending_lock:
        pending.append(entry)
        size = len(pending)

    logger.info(f"Buffered message {entry['id']} ({size}/{BATCH_SIZE} in current batch).")
    if size >= BATCH_SIZE:
        flush_now.set()


async def flush_batch():
    """Send everything currently buffered to Gemini in one call, then notify Discord for
    each message that came back classified as an opportunity."""
    async with pending_lock:
        if not pending:
            return
        batch = pending.copy()
        pending.clear()

    logger.info(f"Flushing batch of {len(batch)} message(s) to Gemini...")
    results = await asyncio.to_thread(analyze_batch, batch)

    for item in batch:
        analysis = results.get(item["id"], DEFAULT_RESULT)

        if not analysis.get("is_opportunity"):
            logger.info(f"Message {item['id']} skipped (not an opportunity).")
            continue

        logger.info(f"Message {item['id']} classified as opportunity: {analysis.get('title')!r}")
        source_link = build_source_link(item["chat_id"], item["id"])
        sent = await asyncio.to_thread(send_notification, analysis, source_link, item["posted_at"])
        if sent:
            logger.info(f"Discord notification sent for message {item['id']}.")
        else:
            logger.error(f"Discord notification FAILED for message {item['id']}.")


async def backfill_test(limit=10):
    """One-off test path: fetch the last `limit` messages already in the group, buffer them,
    and immediately flush as a single batch -- lets you sanity-check the Gemini + Discord
    pipeline without waiting for live traffic."""
    logger.info(f"Backfill test: fetching last {limit} message(s) from group {TELEGRAM_GROUP_ID}...")

    messages = await client.get_messages(TELEGRAM_GROUP_ID, limit=limit)
    messages = list(reversed(messages))  # oldest first, for readable logs

    for message in messages:
        if is_service_message(message):
            logger.info(f"Skipping service message {message.id} (join/leave/pin/etc.).")
            continue
        entry = await build_entry_from_message(message, TELEGRAM_GROUP_ID)
        async with pending_lock:
            pending.append(entry)
        logger.info(f"Backfilled message {entry['id']} into batch buffer.")

    await flush_batch()
    logger.info("Backfill test complete.")


async def batch_flusher():
    """Background loop: flush as soon as the batch fills up (flush_now gets set), or every
    BATCH_INTERVAL_SECONDS if it doesn't, so a lone message during a quiet period still gets
    analyzed in reasonable time."""
    while True:
        try:
            await asyncio.wait_for(flush_now.wait(), timeout=BATCH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        flush_now.clear()
        await flush_batch()


async def main():
    validate_config()
    await client.start()

    if "--test-backfill" in sys.argv:
        limit = 10
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        await backfill_test(limit=limit)
        await client.disconnect()
        return

    logger.info(
        f"Listening for messages in group {TELEGRAM_GROUP_ID} "
        f"(batch size={BATCH_SIZE}, max wait={BATCH_INTERVAL_SECONDS}s)..."
    )
    asyncio.create_task(batch_flusher())
    try:
        await client.run_until_disconnected()
    finally:
        # flush anything left in the buffer on shutdown
        await flush_batch()


if __name__ == "__main__":
    asyncio.run(main())
