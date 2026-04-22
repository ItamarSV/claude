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
You also have a tool to set a reminder when a user explicitly asks you to remind them about something — use the current date provided in the message context to resolve relative times like "tonight", "Sunday", "in 30 minutes". IMPORTANT: when the user specifies an exact time (e.g. "at 7am", "at 20:00"), use EXACTLY that time for iso_time — the current time in the context is only for resolving relative expressions, never use it as the reminder time itself.
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
            "reason": {"type": "STRING", "description": "Why internet access is needed"},
            "question": {"type": "STRING", "description": "Natural language question to ask the user for approval, tailored to their request. E.g. 'The Champions League final was yesterday — want me to look up the result?'"},
        },
        "required": ["reason", "question"],
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
            "confirmation_message": {"type": "STRING", "description": "Natural, conversational confirmation in the SAME LANGUAGE as the user's message. Write as a real person would in WhatsApp chat — NOT as a structured template. Do NOT use '✅ Reminder set for...' format. Examples: 'Done! I'll remind you to call mom tonight at 8.' / 'בסדר, אזכיר לך מחר בבוקר ב-7 לדווח.'"},
            "repeat_question": {"type": "STRING", "description": "Only when repeat_interval='ask': a natural question asking how often to repeat, referencing what the reminder is about. e.g. 'Want me to remind you about this every week? Just say how often.'"},
        },
        "required": ["message", "iso_time", "confirmation_message"],
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
            "confirmation_message": {"type": "STRING", "description": "Natural language confirmation to send after updating, e.g. 'Done! I've set your timezone to London. Your reminders will now fire at the right time for you.'"},
        },
        "required": ["timezone", "confirmation_message"],
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
            "cancellation_message": {"type": "STRING", "description": "Natural language confirmation, referencing what was cancelled, e.g. 'Done, I've cancelled the reminder to call mom.'"},
        },
        "required": ["reminder_id", "cancellation_message"],
    },
)

_base_config = GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[Tool(function_declarations=[_history_func, _web_search_func, _set_reminder_func, _update_timezone_func, _cancel_reminder_func])],
)

async def process_message(group_id: str, sender: str, text: str, sender_jid: str = "", reminders_context: str = "") -> str:
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
                args = dict(fc.args) if fc.args else {}
                return {
                    "type": "web_search",
                    "question": args.get("question", "I need to search the web for this — is that okay?"),
                    "original_message": user_message,
                }

            if fc.name == "set_reminder":
                args = dict(fc.args) if fc.args else {}
                return {
                    "type": "set_reminder",
                    "message": args.get("message", ""),
                    "iso_time": args.get("iso_time", ""),
                    "repeat_interval": args.get("repeat_interval"),
                    "confirmation_message": args.get("confirmation_message"),
                    "repeat_question": args.get("repeat_question"),
                }

            if fc.name == "update_timezone":
                args = dict(fc.args) if fc.args else {}
                return {
                    "type": "update_timezone",
                    "timezone": args.get("timezone", ""),
                    "confirmation_message": args.get("confirmation_message"),
                }

            if fc.name == "cancel_reminder":
                args = dict(fc.args) if fc.args else {}
                return {
                    "type": "cancel_reminder",
                    "reminder_id": args.get("reminder_id", ""),
                    "cancellation_message": args.get("cancellation_message"),
                }

    return _extract_text(response)


async def web_search_call(group_id: str, user_message: str) -> str:
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


_SESSION_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "action": {"type": "STRING", "enum": ["proceed", "cancel", "ignore"]},
        "reply": {"type": "STRING"},
        "interval": {"type": "STRING"},
    },
    "required": ["action", "reply"],
}


async def handle_session_message(
    session_type: str,
    session_question: str,
    session_data: dict,
    user_text: str,
    recent_history: str = "",
) -> dict:
    """
    Classify user's reply against an open dialog session.
    Returns {"action": "proceed"|"cancel"|"ignore", "reply": str, "interval"?: str}
    - proceed: user approved / gave required information
    - cancel:  user declined or wants to move on
    - ignore:  unrelated message — reply answers their question, session stays open silently
    For reminder_repeat proceed, also returns "interval" with the extracted repeat string.
    """
    type_hint = {
        "web_search": (
            "The user was asked to approve an internet search. "
            "If they respond with YES, sure, okay, go ahead, or any affirmative — action MUST be 'proceed'. "
            "If they say no, never mind, or clearly decline — action is 'cancel'. "
            "Only use 'ignore' if the message is completely unrelated (e.g. they ask a different question)."
        ),
        "reminder_repeat": (
            "The user needs to specify a repeat interval for a reminder, or say no to keep it one-time. "
            "If they give any frequency (daily, weekly, every Monday, etc.) — action is 'proceed', extract the interval. "
            "If they say no / one-time / don't repeat — action is 'cancel'. "
            "Only use 'ignore' if the message is completely unrelated."
        ),
    }.get(session_type, "")

    history_section = f"Recent conversation:\n{recent_history}\n\n" if recent_history else ""

    prompt = (
        f"{history_section}"
        f"You have an open dialog session with this user.\n"
        f"Session type: {session_type}\n"
        f"{type_hint}\n"
        f"Question already asked to user: \"{session_question}\"\n\n"
        f"The user just sent: \"{user_text}\"\n\n"
        f"Classify their intent and write a short natural reply in the same language as the user.\n"
        f"For reminder_repeat proceed: include the extracted repeat interval in 'interval'."
    )

    import json as _json
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_SESSION_RESPONSE_SCHEMA,
        ),
    )
    _track_cost("_sessions", response)
    raw = _extract_text(response).strip()
    try:
        return _json.loads(raw)
    except Exception:
        return {"action": "ignore", "reply": raw}


async def generate_action_message(action: str, data: dict) -> str:
    """Generate a natural language message for a completed bot action or edge case."""
    prompts = {
        "reminder_repeat_done": (
            f"You just set a reminder to repeat {data.get('interval')} — "
            f"the reminder is about: '{data.get('message')}', first fires {data.get('fire_str')}. "
            f"Write a short, natural confirmation message in the same language as the reminder text."
        ),
        "reminder_no_repeat": (
            f"The user declined to repeat a reminder about: '{data.get('message')}'. "
            f"Write a short, natural message confirming it stays as a one-time reminder. "
            f"Reply in the same language as the reminder text."
        ),
        "session_already_open": (
            f"The user asked you to {data.get('action_description', 'do something')} but you already have an "
            f"open request waiting for their response. Write a short friendly message asking them to "
            f"finish the current request first, then you can handle the new one."
        ),
    }
    prompt = prompts.get(action, f"Action completed: {action}. Context: {data}. Write a short natural confirmation.")
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )
    return _extract_text(response).strip()


async def generate_timeout_message(session_type: str, session_data: dict, user_name: str) -> str:
    descriptions = {
        "web_search": f"search the web for: {session_data.get('original_message', 'your request')}",
        "reminder_repeat": f"set the repeat interval for the reminder: {session_data.get('message', 'your reminder')}",
    }
    desc = descriptions.get(session_type, "complete your request")
    response = client.models.generate_content(
        model=MODEL,
        contents=(
            f"Write a short, friendly WhatsApp message to @{user_name} saying you didn't get "
            f"their response in time, so you won't {desc}. "
            f"Start with @{user_name}. One sentence. No quotes around the output."
        ),
    )
    return _extract_text(response).strip()


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
