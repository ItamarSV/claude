import base64
import os
from google import genai
from google.genai import types as genai_types
from google.genai.types import (
    GenerateContentConfig,
    Tool,
    FunctionDeclaration,
    GoogleSearch,
)
from history_manager import read_history, read_recent_history
from cost_tracker import record_call

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT = """You are a helpful assistant in a WhatsApp group chat.
You will receive the recent conversation (last 2 hours) as context before each message — use it to stay aware of the ongoing discussion.
You also have a tool to read the full chat history when someone asks about something older than 2 hours.
You also have a tool to request internet access when you need real-time or current information (news, weather, live prices, recent events, etc.).
You also have a tool to set a reminder when a user explicitly asks you to remind them about something — use the current date provided in the message context to resolve relative times like "tonight", "Sunday", "in 30 minutes".
You also have a tool to cancel a reminder when a user asks to delete or remove one — use the pending reminders list provided in the context to identify the correct reminder ID.
You also have a tool to update a user's timezone when they ask to change it.
Keep responses concise and conversational — this is a chat, not a document.
Always reply in the same language as the message you received."""

_history_func = FunctionDeclaration(
    name="get_group_history",
    description=(
        "Retrieve the full chat history of this group (beyond the last 2 hours). "
        "Call this when the question references something older — past decisions, "
        "lists, or events that happened more than 2 hours ago."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "group_id": {"type": "STRING", "description": "The WhatsApp group ID"}
        },
        "required": ["group_id"],
    },
)

_web_search_func = FunctionDeclaration(
    name="request_web_search",
    description=(
        "Call this when you need real-time or current information from the internet "
        "that you don't have in your training data — e.g. today's weather, live news, "
        "current prices, recent events, or anything time-sensitive."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "reason": {"type": "STRING", "description": "Why internet access is needed"}
        },
        "required": ["reason"],
    },
)

_set_reminder_func = FunctionDeclaration(
    name="set_reminder",
    description=(
        "Call this when the user explicitly asks the bot to set a reminder. "
        "Extract the reminder message and time. "
        "Return iso_time as a naive ISO 8601 string in the user's local time (e.g. '2026-04-27T20:00:00'). "
        "Use the current date provided in the message context to resolve relative times. "
        "Set repeat_interval to 'ask' if you detect possible repeat intent but the user hasn't confirmed it."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "message": {"type": "STRING", "description": "What to remind about"},
            "iso_time": {"type": "STRING", "description": "Naive ISO 8601 local datetime, e.g. '2026-04-27T20:00:00'"},
            "repeat_interval": {"type": "STRING", "description": "Repeat interval: 'weekly', 'daily', 'every 30 minutes', 'monthly', 'yearly', etc. Omit if no repeat. Use 'ask' if repeat intent is possible but unclear."},
        },
        "required": ["message", "iso_time"],
    },
)

_update_timezone_func = FunctionDeclaration(
    name="update_timezone",
    description=(
        "Call this when the user asks to update their timezone. "
        "Pass the user's input as-is — it will be resolved to an IANA timezone name."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "timezone": {"type": "STRING", "description": "User's timezone input, e.g. 'London', 'Tel Aviv', 'New York', 'Asia/Jerusalem'"},
        },
        "required": ["timezone"],
    },
)

_cancel_reminder_func = FunctionDeclaration(
    name="cancel_reminder",
    description=(
        "Call this when the user asks to cancel, delete, or remove a reminder. "
        "Use the pending reminders list in the context to identify the correct reminder ID."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "reminder_id": {"type": "STRING", "description": "The short reminder ID (e.g. 'af4e6a90') from the pending reminders list"},
        },
        "required": ["reminder_id"],
    },
)

_base_config = GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[Tool(function_declarations=[_history_func, _web_search_func, _set_reminder_func, _update_timezone_func, _cancel_reminder_func])],
)

# Per-group pending web search state: group_id -> original message
_pending_web_search: dict[str, str] = {}


async def process_message(group_id: str, sender: str, text: str, sender_jid: str = "", reminders_context: str = "") -> str:
    # Check if this is a reply to a pending web search confirmation
    if group_id in _pending_web_search:
        clean = text.strip().lower()
        if clean in ("yes", "yeah", "sure", "yep", "כן", "אוקי", "ok", "web_search_yes", "1"):
            original = _pending_web_search.pop(group_id)
            return await _web_search_call(group_id, original)
        elif clean in ("no", "nope", "לא", "web_search_no", "2"):
            _pending_web_search.pop(group_id, None)
            return "Got it, skipping the web search."
        else:
            _pending_web_search.pop(group_id, None)

    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from timezone_manager import get_user_timezone
    user_tz = get_user_timezone(sender_jid) if sender_jid else "Asia/Jerusalem"
    now_local = _dt.now(ZoneInfo(user_tz))
    time_context = f"[Today is {now_local.strftime('%A %Y-%m-%d')}, current local time is {now_local.strftime('%H:%M')} ({user_tz})]"
    user_message = f"{sender}: {text}"

    recent = read_recent_history(group_id, hours=2)
    extra = ""
    if reminders_context:
        extra = f"\nPending reminders in this group:\n{reminders_context}"
    contents = (
        f"{time_context}{extra}\nRecent conversation (last 2 hours):\n{recent}\n\nNew message:\n{user_message}"
        if recent else f"{time_context}{extra}\n{user_message}"
    )

    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=_base_config,
    )
    _track_cost(group_id, response)

    for part in response.candidates[0].content.parts:
        fc = getattr(part, "function_call", None)
        if fc:
            if fc.name == "get_group_history":
                history = read_history(group_id)
                history_context = (
                    f"Here is the full chat history for this group:\n\n{history}\n\n"
                    if history
                    else "No chat history available yet for this group.\n\n"
                )
                followup = client.models.generate_content(
                    model=MODEL,
                    contents=f"{history_context}Now answer this message:\n{contents}",
                    config=GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
                )
                _track_cost(group_id, followup)
                return _extract_text(followup)

            if fc.name == "request_web_search":
                _pending_web_search[group_id] = user_message
                return {
                    "text": "I need internet access to answer this accurately. Want me to search the web?",
                    "buttons": [
                        {"id": "web_search_yes", "text": "🔍 Yes, search"},
                        {"id": "web_search_no", "text": "❌ No thanks"},
                    ],
                }

            if fc.name == "set_reminder":
                args = dict(fc.args) if fc.args else {}
                return {
                    "type": "set_reminder",
                    "message": args.get("message", ""),
                    "iso_time": args.get("iso_time", ""),
                    "repeat_interval": args.get("repeat_interval"),
                }

            if fc.name == "update_timezone":
                args = dict(fc.args) if fc.args else {}
                return {
                    "type": "update_timezone",
                    "timezone": args.get("timezone", ""),
                }

            if fc.name == "cancel_reminder":
                args = dict(fc.args) if fc.args else {}
                return {
                    "type": "cancel_reminder",
                    "reminder_id": args.get("reminder_id", ""),
                }

    return _extract_text(response)


async def _web_search_call(group_id: str, user_message: str) -> str:
    response = client.models.generate_content(
        model=MODEL,
        contents=user_message,
        config=GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[Tool(google_search=GoogleSearch())],
        ),
    )
    _track_cost(group_id, response)
    return _extract_text(response)


async def resolve_repeat_interval(text: str) -> dict | None:
    prompt = (
        f'Convert this repeat schedule to APScheduler trigger parameters. '
        f'Reply with ONLY a JSON object, no other text.\n\n'
        f'For day-of-week patterns use: {{"type": "cron", "day_of_week": "<apscheduler day_of_week>"}}\n'
        f'  day_of_week uses: mon tue wed thu fri sat sun, comma-separated or ranges\n'
        f'  Examples: "every Monday" -> {{"type": "cron", "day_of_week": "mon"}}\n'
        f'            "every Monday and Sunday" -> {{"type": "cron", "day_of_week": "mon,sun"}}\n'
        f'            "every weekday" -> {{"type": "cron", "day_of_week": "mon-fri"}}\n\n'
        f'For interval patterns use: {{"type": "interval", "weeks": N}} or "days", "hours", "minutes"\n'
        f'  Examples: "weekly" -> {{"type": "interval", "weeks": 1}}\n'
        f'            "every 2 days" -> {{"type": "interval", "days": 2}}\n'
        f'            "daily" -> {{"type": "interval", "days": 1}}\n\n'
        f'Input: "{text}"'
    )
    import json as _json
    response = client.models.generate_content(model=MODEL, contents=prompt)
    raw = _extract_text(response).strip()
    import re as _re
    m = _re.search(r'\{.*\}', raw, _re.DOTALL)
    if m:
        try:
            return _json.loads(m.group())
        except Exception:
            pass
    return None


async def resolve_timezone(text: str) -> str:
    response = client.models.generate_content(
        model=MODEL,
        contents=f'Convert this to an IANA timezone name. Reply with ONLY the IANA identifier (e.g. "Asia/Jerusalem", "Europe/London", "America/New_York"). Input: "{text}"',
        config=GenerateContentConfig(system_instruction="You are a timezone resolver. Reply with just the IANA timezone identifier, nothing else."),
    )
    return _extract_text(response).strip()


async def transcribe_audio(group_id: str, audio_data_b64: str, audio_mime: str) -> str:
    audio_bytes = base64.b64decode(audio_data_b64)
    response = client.models.generate_content(
        model=MODEL,
        contents=[
            genai_types.Part(inline_data=genai_types.Blob(data=audio_bytes, mime_type=audio_mime)),
            genai_types.Part(text="Transcribe this voice message. Return only the spoken words, nothing else."),
        ],
    )
    _track_cost(group_id, response)
    return _extract_text(response).strip()


async def summarize_text(group_id: str, prompt: str) -> str:
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )
    _track_cost(group_id, response)
    return _extract_text(response)


def _extract_text(response) -> str:
    candidates = getattr(response, "candidates", None)
    if not candidates:
        return "Sorry, I couldn't generate a response."
    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) if content else None
    if not parts:
        return "Sorry, I couldn't generate a response."
    for part in parts:
        if hasattr(part, "text") and part.text:
            return part.text.strip()
    return "Sorry, I couldn't generate a response."


def _track_cost(group_id: str, response) -> None:
    usage = getattr(response, "usage_metadata", None)
    if usage:
        record_call(
            group_id=group_id,
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
        )
