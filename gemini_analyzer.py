import json
import logging
import re

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL
from file_extractor import classify_file, extract_text_from_docx, extract_text_from_plain, extract_text_from_xlsx

logger = logging.getLogger(__name__)

# New unified Google GenAI SDK client (replaces the deprecated google.generativeai package).
client = genai.Client(api_key=GEMINI_API_KEY)

_SCHEMA_BLOCK = """{
  "id": string,                  // must exactly match the message id given to you
  "is_opportunity": boolean,
  "title": string,
  "organization": string,
  "summary": string,
  "requirements": [string],
  "survey_link": string,
  "other_links": [string],
  "due_date": string,
  "stipend_or_pay": string,
  "location": string,
  "attachment_text": string,     // verbatim/near-verbatim text visible in any attached image or PDF for this message ("" if no such attachment or no readable text)
  "confidence": number
}"""

_RULES = """Rules:
- Mark "is_opportunity" true for BOTH of these message types:
  1. A new job/internship/hackathon/competition announcement with a clear call to apply (a \
form/survey link, an email to apply to, or explicit application instructions).
  2. A follow-up notification about a recruitment process already underway -- e.g. a shortlist/\
selection announcement for a test or interview (often with a list of names in an attached file), \
a reminder to check a registered email for next steps, a reminder to join a WhatsApp/Discord/\
Telegram group by a deadline, or any other time-sensitive action item tied to that process. These \
often have no survey/application link at all -- that's fine, leave "survey_link" "" in that case.
- If the message is unrelated small talk, a question, a meme, or anything with no recruitment \
process attached (new or ongoing), set "is_opportunity" to false and leave other fields as empty \
defaults ("" or []).
- For a shortlist/reminder message, put the action item(s) themselves in "requirements" (e.g. \
"Check registered email for the assessment link", "Join the WhatsApp group by 5:00 PM"), and put \
the company/organization name in "organization" if it's mentioned.
- "due_date" should capture whatever deadline is given, even a same-day one (e.g. "EOD", "5:00 PM \
today") -- normalize to YYYY-MM-DD only if the date is explicit and unambiguous, otherwise keep \
the original wording.
- Do not invent information that is not present in that message's text or attached file content.
- For "attachment_text": if the message has an image or PDF attachment, transcribe the visible/readable \
text from it as faithfully as you can (this is used for downstream name/contact matching, not display, \
so prioritize completeness over polish). Leave "" if there is no image/PDF attachment or nothing legible.
- Treat each message completely independently -- do not let one message's content influence another's \
classification.
"""

SINGLE_SYSTEM_PROMPT = f"""You are an assistant that monitors a Telegram group chat where students and \
job seekers post internship / job / opportunity announcements, often containing an application \
form link (e.g. forms.gle, forms.google.com, docs.google.com/forms, typeform, airtable, tally.so, \
or a custom application URL) and requirements such as eligibility, skills, deadline, stipend, etc. \
The same group also carries follow-up messages about processes already in motion -- shortlist/\
selection announcements, reminders to check email or join a group, and other deadline-driven \
next steps -- which matter just as much even though they don't contain a new application link.

Respond with ONLY valid JSON (no markdown fences, no commentary) matching this schema exactly \
(omit the "id" field):
{_SCHEMA_BLOCK}

{_RULES}"""

BATCH_SYSTEM_PROMPT = f"""You are an assistant that monitors a Telegram group chat where students and \
job seekers post internship / job / opportunity announcements, often containing an application \
form link (e.g. forms.gle, forms.google.com, docs.google.com/forms, typeform, airtable, tally.so, \
or a custom application URL) and requirements such as eligibility, skills, deadline, stipend, etc. \
The same group also carries follow-up messages about processes already in motion -- shortlist/\
selection announcements, reminders to check email or join a group, and other deadline-driven \
next steps -- which matter just as much even though they don't contain a new application link.

You will receive MULTIPLE Telegram messages in a single request. Each message is delimited by a \
marker line exactly like:
=== MESSAGE id=<ID> ===
followed by that message's text, and then any attached file content belonging to that specific \
message (still before the next "=== MESSAGE id=..." marker).

Analyze each message independently and respond with ONLY a valid JSON array (no markdown fences, \
no commentary), containing exactly one object per message, in any order, each matching this schema:
{_SCHEMA_BLOCK}

The "id" field in each object MUST exactly match the id from that message's marker line.

{_RULES}"""

DEFAULT_RESULT = {
    "is_opportunity": False,
    "title": "",
    "organization": "",
    "summary": "",
    "requirements": [],
    "survey_link": "",
    "other_links": [],
    "due_date": "",
    "stipend_or_pay": "",
    "location": "",
    "attachment_text": "",
    "confidence": 0.0,
}


def build_attachment_parts(attachments):
    """attachments: list of {"path": str, "filename": str}.

    Returns (parts, local_text):
      - parts: Gemini content parts (PDFs/images uploaded via the Files API immediately --
        handles persist ~48h, so it's safe to build these ahead of time and reuse them in a
        later batched call; docx/plain text is inlined as labeled text parts).
      - local_text: the raw extracted body text for docx/plain-text/xlsx attachments (no
        labels/filenames), for local fuzzy-matching use -- image/PDF text isn't available
        locally, it comes back from Gemini as "attachment_text" in the analysis result instead.
    """
    parts = []
    local_text_chunks = []

    for att in attachments:
        path, filename = att["path"], att["filename"]
        kind = classify_file(path)

        if kind == "gemini_native":
            try:
                parts.append(client.files.upload(file=path))
            except Exception as e:
                logger.error(f"Failed to upload {path} to Gemini: {e}")
                parts.append(f"\n[Attached file '{filename}' could not be uploaded for analysis]\n")
        elif kind == "docx":
            text = extract_text_from_docx(path)
            if text:
                parts.append(f"\n[Attached file '{filename}' content]:\n{text}\n")
                local_text_chunks.append(text)
        elif kind == "plain_text":
            text = extract_text_from_plain(path)
            if text:
                parts.append(f"\n[Attached file '{filename}' content]:\n{text}\n")
                local_text_chunks.append(text)
        elif kind == "xlsx":
            text = extract_text_from_xlsx(path)
            if text:
                local_text_chunks.append(text)
        else:
            parts.append(f"\n[Attached file '{filename}': unsupported file type, content not read]\n")

    return parts, "\n".join(local_text_chunks)


def _generate(system_prompt, content_parts):
    """Shared call into the Gemini API, requesting JSON output directly."""
    return client.models.generate_content(
        model=GEMINI_MODEL,
        contents=content_parts,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
        ),
    )


# Matches a backslash NOT followed by a character that's valid after "\" in JSON
# (", \, /, b, f, n, r, t, u). Gemini occasionally emits a literal stray backslash inside a
# string value -- most often in "attachment_text" (OCR'd dates like "31\08\2026", file paths,
# stray symbols) -- which json.loads rejects outright with "Invalid \escape".
_INVALID_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def _parse_json(raw: str):
    """json.loads with one fallback: if it fails specifically on an invalid escape sequence,
    double up any stray backslashes and retry once before giving up."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        if "escape" not in str(e).lower():
            raise
        logger.warning(f"Gemini JSON had an invalid escape sequence, attempting to repair: {e}")
        fixed = _INVALID_ESCAPE_RE.sub(r"\\\\", raw)
        return json.loads(fixed)  # let this raise if still broken -- caller logs + falls back


def analyze_message(message_text: str, attachments=None) -> dict:
    """Single-message analysis (kept around for testing / very low-traffic setups)."""
    attachments = attachments or []
    content_parts = [f"Telegram message text:\n{message_text or '(empty message, see attachments)'}"]
    parts, _local_text = build_attachment_parts(attachments)
    content_parts.extend(parts)

    raw = ""
    try:
        response = _generate(SINGLE_SYSTEM_PROMPT, content_parts)
        raw = (response.text or "").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = _parse_json(raw)
        result = dict(DEFAULT_RESULT)
        result.update(data)
        return result
    except json.JSONDecodeError as e:
        logger.error(f"Gemini returned non-JSON response: {e}\nRaw (truncated): {raw[:500]}")
        return dict(DEFAULT_RESULT)
    except Exception as e:
        logger.error(f"Gemini analysis failed: {e}")
        return dict(DEFAULT_RESULT)


def analyze_batch(items) -> dict:
    """
    items: list of {"id": str, "text": str, "parts": list} -- "parts" is the pre-built output
    of build_attachment_parts() for that message.

    Returns a dict mapping id -> result dict (schema of DEFAULT_RESULT). Any id missing from
    Gemini's response, or the whole call failing, falls back to DEFAULT_RESULT for that id so a
    bad/partial batch response never silently drops a message -- it's just treated as "not an
    opportunity" and logged.
    """
    if not items:
        return {}

    content_parts = []
    for item in items:
        content_parts.append(
            f"=== MESSAGE id={item['id']} ===\n{item['text'] or '(empty message, see attachments)'}"
        )
        content_parts.extend(item.get("parts", []))

    results = {item["id"]: dict(DEFAULT_RESULT) for item in items}
    raw = ""
    try:
        response = _generate(BATCH_SYSTEM_PROMPT, content_parts)
        raw = (response.text or "").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = _parse_json(raw)
        if not isinstance(data, list):
            raise ValueError("Expected a JSON array from batch analysis")
        for entry in data:
            entry_id = str(entry.get("id", ""))
            if entry_id in results:
                merged = dict(DEFAULT_RESULT)
                merged.update(entry)
                results[entry_id] = merged
            else:
                logger.warning(f"Gemini batch response contained unknown id {entry_id!r}, ignoring")
        missing = [i for i in results if i not in {str(e.get('id', '')) for e in data}]
        if missing:
            logger.warning(f"Gemini batch response missing ids {missing}, defaulting to not-an-opportunity")
        return results
    except json.JSONDecodeError as e:
        logger.error(f"Gemini batch returned non-JSON response: {e}\nRaw (truncated): {raw[:800]}")
        return results
    except Exception as e:
        logger.error(f"Gemini batch analysis failed: {e}")
        return results