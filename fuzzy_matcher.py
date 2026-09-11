import logging

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

# Ignore candidate strings shorter than this -- short values (e.g. a "1" in a phone number
# fragment) produce noisy false positives with partial-ratio matching.
MIN_CANDIDATE_LEN = 3

# Minimum number of distinct search_values that must fuzzy-match for a directory entry
# to be counted as a mention -- a single matching field (e.g. just a common first name)
# is too noisy on its own.
NUM_ENTITIES = 2


def find_mentions(text: str, directory, threshold: int = 90):
    """
    text: combined searchable text for one message (message body + any extracted
          attachment text -- docx/plain text content and/or Gemini's image/PDF transcription).
    directory: list of {"name", "discord_id", "search_values"} from notion_directory.get_directory().
    threshold: rapidfuzz partial_ratio score (0-100) required to count as a match.

    Returns a list of {"name", "discord_id"} for directory members with at least
    NUM_ENTITIES distinct Notion field values that fuzzy-match somewhere in `text`,
    deduped by discord_id. Works against whatever fields exist in the directory --
    no field names are hardcoded here.
    """
    if not text or not text.strip() or not directory:
        return []

    text_lower = text.lower()
    matched = {}

    for entry in directory:
        if entry["name"] in matched:
            continue

        hit_count = 0
        for candidate in entry["search_values"]:
            candidate = candidate.strip()
            if len(candidate) < MIN_CANDIDATE_LEN:
                continue
            score = fuzz.partial_ratio(candidate.lower(), text_lower)
            if score >= threshold:
                hit_count += 1
                logger.debug(f"Mention match: '{candidate}' -> {entry['name']} (score={score})")
                if hit_count >= NUM_ENTITIES:
                    matched[entry["name"]] = {"name": entry["name"], "discord_id": entry["discord_id"]}
                    break

    return list(matched.values())