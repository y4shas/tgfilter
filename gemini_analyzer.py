import json
import logging

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL
from file_extractor import classify_file, extract_text_from_docx, extract_text_from_plain

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
  "confidence": number
}"""

_RULES = """Rules:
- If nothing resembling an application process, form link, or job/internship/hackathon offer is present, \
set "is_opportunity" to false and leave other fields as empty defaults ("" or []).
- Only mark "is_opportunity" true if there's a clear call to apply (a form/survey link, an email to apply to, \
or explicit application instructions).
- Do not invent information that is not present in that message's text or attached file content.
- Treat each message completely independently -- do not let one message's content influence another's \
classification.
"""

SINGLE_SYSTEM_PROMPT = f"""You are an assistant that monitors a Telegram group chat where students and \
job seekers post internship / job / opportunity announcements, often containing an application \
form link (e.g. forms.gle, forms.google.com, docs.google.com/forms, typeform, airtable, tally.so, \
or a custom application URL) and requirements such as eligibility, skills, deadline, stipend, etc.

Respond with ONLY valid JSON (no markdown fences, no commentary) matching this schema exactly \
(omit the "id" field):
{_SCHEMA_BLOCK}

{_RULES}"""

BATCH_SYSTEM_PROMPT = f"""You are an assistant that monitors a Telegram group chat where students and \
job seekers post internship / job / opportunity announcements, often containing an application \
form link (e.g. forms.gle, forms.google.com, docs.google.com/forms, typeform, airtable, tally.so, \
or a custom application URL) and requirements such as eligibility, skills, deadline, stipend, etc.

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
    "confidence": 0.0,
}


def build_attachment_parts(attachments):
    """attachments: list of {"path": str, "filename": str}. Returns Gemini content parts.

    PDFs/images are uploaded via the Files API immediately -- uploaded file handles persist for
    ~48h, so it's safe to build these ahead of time and reuse them in a later batched call.
    """
    parts = []
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
        elif kind == "plain_text":
            text = extract_text_from_plain(path)
            if text:
                parts.append(f"\n[Attached file '{filename}' content]:\n{text}\n")
        else:
            parts.append(f"\n[Attached file '{filename}': unsupported file type, content not read]\n")

    return parts


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


def analyze_message(message_text: str, attachments=None) -> dict:
    """Single-message analysis (kept around for testing / very low-traffic setups)."""
    attachments = attachments or []
    content_parts = [f"Telegram message text:\n{message_text or '(empty message, see attachments)'}"]
    content_parts.extend(build_attachment_parts(attachments))

    raw = ""
    try:
        response = _generate(SINGLE_SYSTEM_PROMPT, content_parts)
        raw = (response.text or "").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
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
        data = json.loads(raw)
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