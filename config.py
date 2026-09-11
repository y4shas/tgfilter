import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_GROUP_ID = int(os.getenv("TELEGRAM_GROUP_ID", "0"))
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "job_opportunities_session")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

# Batching: flush to Gemini once this many messages are buffered...
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))
# ...or after this many seconds since the last flush, whichever comes first (so a lone
# message during a quiet period doesn't sit unanalyzed indefinitely).
BATCH_INTERVAL_SECONDS = int(os.getenv("BATCH_INTERVAL_SECONDS", "30"))

# --- Notion mention directory (optional) ---
# If either is unset, the mention feature is silently disabled.
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")
# How long to cache the directory before re-fetching from Notion.
NOTION_CACHE_TTL_SECONDS = int(os.getenv("NOTION_CACHE_TTL_SECONDS", "300"))
# rapidfuzz partial_ratio score (0-100) required to count as a match.
MENTION_FUZZY_THRESHOLD = int(os.getenv("MENTION_FUZZY_THRESHOLD", "87"))

MENTIONS_ENABLED = bool(NOTION_API_KEY and NOTION_DATABASE_ID)

_REQUIRED_VARS = {
    "TELEGRAM_API_ID": TELEGRAM_API_ID,
    "TELEGRAM_API_HASH": TELEGRAM_API_HASH,
    "TELEGRAM_GROUP_ID": TELEGRAM_GROUP_ID,
    "GEMINI_API_KEY": GEMINI_API_KEY,
    "DISCORD_WEBHOOK_URL": DISCORD_WEBHOOK_URL,
}


def validate_config():
    """Raise a clear error if any required .env variable is missing/empty."""
    missing = [k for k, v in _REQUIRED_VARS.items() if not v]
    if missing:
        raise EnvironmentError(
            f"Missing required .env variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill these in."
        )
