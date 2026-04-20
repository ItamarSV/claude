import os
from google import genai
from google.genai.types import (
    GenerateContentConfig,
    Tool,
    FunctionDeclaration,
    GoogleSearch,
)
from history_manager import read_history
from cost_tracker import record_call

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT = """You are a helpful assistant in a WhatsApp group chat.
You have a tool to read the full chat history of this group when questions require it.
You also have a tool to request internet access when you need real-time or current information
(news, weather, live prices, recent events, etc.) that you don't have in your training data.
Keep responses concise and conversational — this is a chat, not a document.
When using chat history, reference specific details to show you actually read it."""

_history_func = FunctionDeclaration(
    name="get_group_history",
    description=(
        "Retrieve the full chat history of the current WhatsApp group. "
        "Call this when the question references past conversations, "
        "decisions, lists, or anything that happened in this group before."
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

_base_config = GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[Tool(function_declarations=[_history_func, _web_search_func])],
)

# Per-group pending web search state: group_id -> original message
_pending_web_search: dict[str, str] = {}


async def process_message(group_id: str, sender: str, text: str) -> str:
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

    user_message = f"{sender}: {text}"

    response = client.models.generate_content(
        model=MODEL,
        contents=user_message,
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
                    contents=f"{history_context}Now answer this message:\n{user_message}",
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


def _extract_text(response) -> str:
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            return part.text.strip()
    return "Sorry, I couldn't generate a response."


def _track_cost(group_id: str, response) -> None:
    usage = getattr(response, "usage_metadata", None)
    if usage:
        record_call(
            group_id=group_id,
            input_tokens=getattr(usage, "prompt_token_count", 0),
            output_tokens=getattr(usage, "candidates_token_count", 0),
        )
