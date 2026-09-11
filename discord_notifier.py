import logging
from datetime import datetime, timezone

import requests

from config import DISCORD_WEBHOOK_URL

logger = logging.getLogger(__name__)

MAX_FIELD_LEN = 1024
MAX_DESC_LEN = 4096

def remove_duplicates(input_list):
    return list(dict.fromkeys(input_list))

def _truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_embed(analysis: dict, source_link: str = None, posted_at: datetime = None, mentions=None) -> dict:
    mentions = mentions or []
    fields = []
    color = None

    if analysis.get("organization"):
        fields.append({"name": "Organization", "value": _truncate(analysis["organization"], MAX_FIELD_LEN), "inline": True})

    if analysis.get("location"):
        fields.append({"name": "Location", "value": _truncate(analysis["location"], MAX_FIELD_LEN), "inline": True})

    if analysis.get("stipend_or_pay"):
        fields.append({"name": "Stipend / Pay", "value": _truncate(analysis["stipend_or_pay"], MAX_FIELD_LEN), "inline": True})

    fields.append({
        "name": "⏰ Due Date",
        "value": _truncate(analysis.get("due_date") or "Not specified", MAX_FIELD_LEN),
        "inline": True,
    })

    if analysis.get("requirements"):
        req_text = "\n".join(f"• {r}" for r in analysis["requirements"])
        fields.append({"name": "Requirements", "value": _truncate(req_text, MAX_FIELD_LEN), "inline": False})

    if analysis.get("survey_link"):
        color = 0x3498DB  # blue for opportunities with survey links
        fields.append({"name": "📝 Apply Here", "value": analysis["survey_link"], "inline": False})

    if analysis.get("other_links"):
        fields.append({"name": "Other Links", "value": "\n".join(analysis["other_links"]), "inline": False})

    if mentions:
        color = 0xE67E22  # orange for mentions-only
        names = ", ".join(m["name"] for m in mentions)
        fields.append({"name": "🔔 Mentioned", "value": _truncate(names, MAX_FIELD_LEN), "inline": False})

    if source_link:
        fields.append({"name": "Source Message", "value": source_link, "inline": False})

    return {
        "title": analysis.get("title") or "New Opportunity",
        "description": _truncate(analysis.get("summary", ""), MAX_DESC_LEN),
        "color": color or 0x2ECC71,
        "fields": fields,
        "footer": {"text": f"Confidence: {round((analysis.get('confidence') or 0) * 100)}%"},
        "timestamp": (posted_at or datetime.now(timezone.utc)).isoformat(),
    }


def send_notification(analysis: dict, source_link: str = None, posted_at: datetime = None, mentions=None) -> bool:
    if not DISCORD_WEBHOOK_URL:
        logger.error("DISCORD_WEBHOOK_URL not configured, cannot send notification")
        return False

    mentions = mentions or []
    embed = build_embed(analysis, source_link=source_link, posted_at=posted_at, mentions=mentions)

    payload = {"embeds": [embed]}

    # Actual @mention pings only fire from the webhook's top-level "content" -- mentions
    # written inside an embed are just plain text and don't notify anyone.
    if mentions:
        mentioned_ids = remove_duplicates([m["discord_id"] for m in mentions])
        payload["content"] = " ".join(f"<@{m}>" for m in mentioned_ids)
        payload["allowed_mentions"] = {"parse": [], "users": mentioned_ids}

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        if resp.status_code >= 300:
            logger.error(f"Discord webhook failed: {resp.status_code} {resp.text}")
            return False
        return True
    except Exception as e:
        logger.error(f"Failed to send Discord notification: {e}")
        return False
