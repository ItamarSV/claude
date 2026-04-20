import os
import google.generativeai as genai
from google.generativeai.types import Tool, FunctionDeclaration
from history_manager import read_history
from cost_tracker import record_call

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-1.5-pro"

SYSTEM_PROMPT = """You are a helpful assistant in a WhatsApp group chat.
You have access to Google Search for real-time information (weather, news, facts, etc.).
You also have a tool to read the full chat history of this group when questions require it.
Keep responses concise and conversational — this is a chat, not a document.
When using chat history, reference specific details to show you actually read it."""

_history_tool = Tool(function_declarations=[
    FunctionDeclaration(
        name="get_group_history",
        description=(
            "Retrieve the full chat history of the current WhatsApp group. "
            "Call this when the question references past conversations, "
            "decisions, lists, or anything that happened in this group before."
        ),
        parameters={
            "type": "object",
            "properties": {
                "group_id": {
                    "type": "string",
                    "description": "The WhatsApp group ID",
                }
            },
            "required": ["group_id"],
        },
    )
])

_search_tool = Tool(google_search_retrieval={})


async def process_message(group_id: str, sender: str, text: str) -> str:
    model = genai.GenerativeModel(
        model_name=MODEL,
        system_instruction=SYSTEM_PROMPT,
        tools=[_history_tool, _search_tool],
    )

    user_message = f"{sender}: {text}"

    # First call — let Gemini decide if it needs history or search
    response = model.generate_content(user_message)
    _track_cost(group_id, response)

    # Check if Gemini wants to call get_group_history
    for part in response.candidates[0].content.parts:
        if part.function_call and part.function_call.name == "get_group_history":
            history = read_history(group_id)
            history_context = (
                f"Here is the full chat history for this group:\n\n{history}\n\n"
                if history
                else "No chat history available yet for this group.\n\n"
            )
            # Second call — inject history as context
            followup = model.generate_content(
                f"{history_context}Now answer this message:\n{user_message}"
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
            input_tokens=usage.prompt_token_count,
            output_tokens=usage.candidates_token_count,
        )
