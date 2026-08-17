"""
One-off helper: lists your Telegram dialogs (chats/groups) with their IDs,
so you can find the correct TELEGRAM_GROUP_ID to put in .env.

Usage:
    python get_group_id.py
"""
import asyncio

from telethon import TelegramClient

from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_NAME


async def main():
    client = TelegramClient(TELEGRAM_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start()
    print(f"{'ID':>15}  {'Type':<10}  Name")
    print("-" * 60)
    async for dialog in client.iter_dialogs():
        kind = "group" if dialog.is_group else ("channel" if dialog.is_channel else "user")
        print(f"{dialog.id:>15}  {kind:<10}  {dialog.name}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
