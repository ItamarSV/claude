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

MODEL = "gemini-1.5-pro"

SYSTEM_PROMPT = """You are a helpful assistant in a WhatsApp group chat.
You have access to Google Search for real-time information (weather, news, facts, etc.).
You also have a tool to read the full chat history of this group when questions require it.
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
            "group_id": {
                "type": "STRING",
                "description": "The WhatsApp group ID",
            }
        },
        "required": ["group_id"],
    },
)

_tools = [
    Tool(function_declarations=[_history_func]),
    Tool(google_search=GoogleSearch()),
]

_base_config = GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=_tools,
)


async def process_message(group_id: str, sender: str, text: str) -> str:
    user_message = f"{sender}: {text}"

    response = client.models.generate_content(
        model=MODEL,
        contents=user_message,
        config=_base_config,
    )
    _track_cost(group_id, response)

    for part in response.candidates[0].content.parts:
        fc = getattr(part, "function_call", None)
        if fc and fc.name == "get_group_history":
            history = read_history(group_id)
            history_context = (
                f"Here is the full chat history for this group:\n\n{history}\n\n"
                if history
                else "No chat history available yet for this group.\n\n"
            )
            followup = client.models.generate_content(
                model=MODEL,
                contents=f"{history_context}Now answer this message:\n{user_message}",
                config=GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=[Tool(google_search=GoogleSearch())],
                ),
            )
            _track_cost(group_id, followup)
            return _extract_text(followup)

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
