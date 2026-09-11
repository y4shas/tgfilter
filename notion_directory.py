import logging
import time

import requests

from config import NOTION_API_KEY, NOTION_DATABASE_ID, NOTION_CACHE_TTL_SECONDS

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

_cache = {"entries": [], "fetched_at": 0.0}


def _headers():
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _property_to_strings(prop: dict):
    """Flatten a single Notion property value into a list of plain search strings.

    Handles every common property type generically so new columns added later in Notion
    (a new field, a renamed one, etc.) get picked up automatically without code changes --
    nothing here is keyed off a specific field name except the special-cased title/discord
    detection in _extract_row below.
    """
    ptype = prop.get("type")
    value = prop.get(ptype)

    if value is None:
        return []

    if ptype in ("title", "rich_text"):
        text = "".join(chunk.get("plain_text", "") for chunk in value)
        return [text] if text.strip() else []

    if ptype in ("email", "phone_number", "url"):
        return [value] if value else []

    if ptype == "number":
        return [str(value)] if value is not None else []

    if ptype in ("select", "status"):
        return [value.get("name", "")] if value else []

    if ptype == "multi_select":
        return [opt.get("name", "") for opt in value if opt.get("name")]

    if ptype == "people":
        return [p.get("name", "") for p in value if p.get("name")]

    if ptype == "date":
        start = value.get("start") if isinstance(value, dict) else None
        return [start] if start else []

    if ptype == "checkbox":
        return []  # booleans aren't useful as fuzzy-match candidates

    # Unknown/unsupported property type (files, relations, formulas, etc.) -- skip rather
    # than crash; new simple text-like columns are covered by the cases above.
    return []


def _extract_row(page: dict):
    """Turn one Notion database row into {"name", "discord_id", "search_values": [...]}."""
    props = page.get("properties", {})

    name = ""
    discord_id = ""
    search_values = []

    for prop_name, prop in props.items():
        values = [v.strip() for v in _property_to_strings(prop) if v and v.strip()]
        if not values:
            continue

        search_values.extend(values)

        if prop.get("type") == "title" and not name:
            name = values[0]
        if "discord" in prop_name.lower() and not discord_id:
            discord_id = values[0]

    return {
        "name": name or "Unknown",
        "discord_id": discord_id,
        "search_values": list(dict.fromkeys(search_values)),  # dedupe, preserve order
    }


def _fetch_all_pages():
    entries = []
    payload = {"page_size": 100}
    url = f"{NOTION_API_BASE}/databases/{NOTION_DATABASE_ID}/query"

    while True:
        try:
            resp = requests.post(url, headers=_headers(), json=payload, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to query Notion database: {e}")
            break

        data = resp.json()
        for page in data.get("results", []):
            entry = _extract_row(page)
            if entry["discord_id"]:
                entries.append(entry)
            else:
                logger.debug(f"Skipping Notion row {page.get('id')}: no Discord ID value found")

        if not data.get("has_more"):
            break
        payload["start_cursor"] = data.get("next_cursor")

    return entries


def get_directory(force_refresh: bool = False):
    """Return the cached directory (list of {"name", "discord_id", "search_values"}),
    refreshing from Notion if the cache is stale or force_refresh is set."""
    now = time.time()
    if force_refresh or (now - _cache["fetched_at"]) > NOTION_CACHE_TTL_SECONDS:
        entries = _fetch_all_pages()
        if entries or force_refresh:
            _cache["entries"] = entries
            _cache["fetched_at"] = now
            logger.info(f"Notion mention directory refreshed: {len(entries)} member(s) with a Discord ID.")
        else:
            logger.warning("Notion directory fetch returned nothing; keeping previous cache.")
    return _cache["entries"]

