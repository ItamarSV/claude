# Bot Service

**Files:** `bot-service/main.py`, `gemini_client.py`, `history_manager.py`, `cost_tracker.py`
**Runtime:** Python 3.12, FastAPI + Uvicorn

## Webhook Flow (`main.py`)
1. `POST /webhook` receives `{group_id, sender, text, timestamp}` from whatsapp-service
2. `append_message()` writes the message to the group's history file immediately
3. `process_message()` calls Gemini and gets a reply
4. Reply is POSTed to whatsapp-service `POST /send`

History is always saved regardless of whether the bot can respond (Gemini errors don't lose history).

## Gemini Tool Use Pattern (`gemini_client.py`)
Two tools are always registered on every call:
- `get_group_history(group_id)` — reads the group's `.txt` history file
- `google_search_retrieval` — built-in Google Search grounding for real-time info

**Two-phase call pattern:**
1. **First call:** send the user's message to Gemini with both tools available
2. Check the response parts for a `function_call` named `get_group_history`
3. **If tool called:** load the history file → make a second call with history injected as context
4. **If no tool call:** use the first response directly

Gemini decides autonomously whether history is needed based on the question. Weather, math, and current events → uses Search, ignores history tool. "What did we decide last week?" → calls `get_group_history`.

The second call does NOT re-offer the `get_group_history` tool (history is already injected as text), which prevents infinite tool-call loops.

## History Files (`history_manager.py`)
- Stored in `group_histories/{sanitized_group_id}.txt`
- One line per message: `[2026-04-19 14:32] John: hey what's the plan?`
- Group ID is sanitized (special chars → `_`) for safe filenames
- **Write queue:** each group file has its own `asyncio.Lock`. Multiple simultaneous messages to the same group are serialized to prevent file corruption.
- `read_history()` is synchronous (fine — only called from within an async context, and it's fast I/O)

## Cost Tracking (`cost_tracker.py`)
Called by `gemini_client.py` after every Gemini API response via `_track_cost()`.

**Pricing tiers** (Gemini 1.5 Pro):
- Tier 1 (≤128K total tokens per call): input $1.25/1M, output $5.00/1M
- Tier 2 (>128K total tokens per call): input $2.50/1M, output $10.00/1M

Tier is determined by `input_tokens + output_tokens` for that specific call. A call that loads a large history file will likely hit Tier 2.

Each call appends one line to `cost_logs/YYYY-MM.txt`:
```
[2026-04-19 14:32] group=120363abc_g_us tier=1 in=1240 out=320 cost=$0.00210
```

`get_monthly_summary(year, month)` parses the file and returns totals + per-group breakdown — ready to be formatted into a WhatsApp message.

## Google Search Grounding
Enabled by passing `google_search_retrieval={}` as a tool. Gemini automatically decides when to invoke it — no explicit tool call appears in the response parts. It's seamless: if Gemini needs current information, it searches and incorporates results without any extra code needed.
