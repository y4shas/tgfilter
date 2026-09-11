# Telegram Job Opportunity Watcher

Listens to a Telegram group chat, uses Gemini to detect and summarize job/internship/opportunity
posts (including details buried in attached PDFs/images/docx files), and posts a formatted
notification — with survey link, requirements, and due date — to a Discord channel via webhook.
Non-opportunity messages are silently ignored. Optionally, it cross-references a Notion database
of members and @mentions anyone it fuzzy-matches in the message/attachments.

## How it works

1. `main.py` opens a Telethon client and listens for `NewMessage` events in `TELEGRAM_GROUP_ID`.
2. As each message arrives, any attachment is downloaded and immediately converted into Gemini
   content: PDFs/images are uploaded to Gemini's Files API (handles persist ~48h), `.docx` is
   parsed locally with `python-docx`, plain text files are read directly. The local file is then
   deleted -- only the extracted/uploaded content (and, for docx/plain text, a local copy of the
   raw text) is kept in memory.
3. The message (text + extracted content) is added to an in-memory **batch buffer** instead of
   being analyzed right away.
4. The buffer is flushed to Gemini in a single request whenever either:
   - it reaches `BATCH_SIZE` messages, or
   - `BATCH_INTERVAL_SECONDS` has passed since the last flush (so a lone message during a quiet
     period doesn't sit around unanalyzed).
   One Gemini call analyzes the whole batch at once and returns a JSON array with one result per
   message (matched back up by message id), each with `is_opportunity` plus, if true, title, org,
   summary, requirements, survey/application link, other links, due date, stipend, location, and
   a transcription of any image/PDF attachment text (`attachment_text`).
5. For each message classified as an opportunity, if a Notion mention directory is configured
   (see below), its text + local attachment text + `attachment_text` is fuzzy-matched (via
   `rapidfuzz`) against every field of every row in your Notion database. Any matched member gets
   `<@discord_id>`-mentioned in the notification.
6. Every message in the batch classified as an opportunity gets its own Discord notification via
   `discord_notifier.py`; the rest are silently skipped.
7. On shutdown (Ctrl+C), whatever's left in the buffer is flushed before exiting.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

- **TELEGRAM_API_ID / TELEGRAM_API_HASH** — from https://my.telegram.org/apps
- **TELEGRAM_GROUP_ID** — run `python get_group_id.py` after setting the API id/hash, log in with
  your phone number when prompted, and copy the ID of the target group from the printed list
  (supergroup IDs look like `-1001234567890`).
- **GEMINI_API_KEY** — from https://aistudio.google.com/apikey
- **DISCORD_WEBHOOK_URL** — Discord: Server Settings → Integrations → Webhooks → New Webhook → Copy URL

### Optional: @mention people from a Notion database

If you want the bot to `@mention` group members it recognizes in a message (or one of its
attachments), point it at a Notion database:

1. Create an internal integration at https://www.notion.so/my-integrations and copy its secret
   into `NOTION_API_KEY`.
2. Open your database (e.g. the "Telegram Mention DB" with Name / USN / Email / Phone / Discord ID
   columns), click **`...`** → **Connections** → add your integration.
3. Copy the database ID out of its URL — `notion.so/<workspace>/<DATABASE_ID>?v=...` — into
   `NOTION_DATABASE_ID`.
4. Make sure one column's name contains "discord" (e.g. "Discord ID") holding each member's
   numeric Discord user ID — that's the only column name the code looks for specifically.

Every other column (Name, USN, Email, Phone, or anything you add later) is read generically and
used as a fuzzy-match candidate automatically — nothing is hardcoded to specific field names, so
adding new columns in Notion just works without touching the code. Leave `NOTION_API_KEY` /
`NOTION_DATABASE_ID` blank to disable the feature entirely.

## Run

### Local Python run

```bash
python main.py
```

On first run Telethon will prompt for your phone number + login code (and 2FA password if set)
to create a local session file (`job_opportunities_session.session`). Subsequent runs reuse it
— keep that file private, it's an authenticated login.

Test the pipeline against existing messages locally:

```bash
python main.py --test-backfill              # last 10 messages
python main.py --test-backfill --limit 25    # last 25 messages
```

### Run with Docker

Build the image and pass your environment variables from a `.env` file:

```bash
cp .env.example .env
# edit .env and fill in your Telegram, Gemini, Discord, and optional Notion values

docker build -t tgfilter .
docker run --rm --env-file .env tgfilter
```

If you want the Telethon session to persist across container restarts, mount the session file:

```bash
docker run --rm --env-file .env \
  -v "${PWD}/job_opportunities_session.session:/app/job_opportunities_session.session" \
  tgfilter
```

You can also run the backfill test inside Docker:

```bash
docker run --rm --env-file .env tgfilter --test-backfill --limit 25
```

### Test the pipeline against existing messages

Instead of waiting for live traffic, you can pull the last N messages already in the group,
buffer them, and flush them as one batch right away:

```bash
python main.py --test-backfill              # last 10 messages
python main.py --test-backfill --limit 25    # last 25 messages
```

This exercises the full pipeline (attachment download → Gemini batch analysis → Discord
notifications for anything classified as an opportunity) and exits -- it doesn't start the live
listener.

## Notes / things to tune

- **Detection accuracy**: the classification prompt lives in `gemini_analyzer.py::SYSTEM_PROMPT`.
  Tighten or loosen the "what counts as an opportunity" rules there if you get false
  positives/negatives.
- **File types**: PDFs and images are sent to Gemini natively. `.docx` is parsed via `python-docx`.
  Other file types (e.g. `.xlsx`, `.pptx`) currently aren't parsed for content — add a branch in
  `file_extractor.py` / `gemini_analyzer.py::_build_file_parts` if your group shares those.
  Google Form links (forms.gle etc.) are detected from the message/file text itself, not by
  visiting the link — Gemini isn't browsing the actual form.
  is
- **Due dates**: Gemini is asked to normalize to `YYYY-MM-DD` when it can infer the year/month,
  otherwise it keeps the original wording (e.g. "this Friday") — treat these as best-effort.
- **Batching**: tune `BATCH_SIZE` and `BATCH_INTERVAL_SECONDS` in `.env` to trade off latency vs.
  Gemini call volume. Small group / want near-instant notifications: `BATCH_SIZE=1`. High-traffic
  group: raise `BATCH_SIZE` and/or `BATCH_INTERVAL_SECONDS` to batch more messages per call.
- **Mentions**: matching is done with `rapidfuzz.fuzz.partial_ratio` against every text-like
  column value in your Notion database (`fuzzy_matcher.py`). Raise `MENTION_FUZZY_THRESHOLD`
  (closer to 100) if you get false-positive mentions, or lower it if real matches are being
  missed. The directory is cached for `NOTION_CACHE_TTL_SECONDS` so adding a new row in Notion
  can take up to that long to show up. Image/PDF attachment content is only searchable because
  Gemini transcribes it into `attachment_text` during the same batch call -- docx/plain-text
  attachments are matched from the locally-extracted copy instead.
