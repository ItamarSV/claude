# Bot Service

**Files:** `bot-service/main.py`, `gemini_client.py`, `history_manager.py`, `cost_tracker.py`
**Runtime:** Python 3.12, FastAPI + Uvicorn, runs in a `venv/`

## Webhook Flow (`main.py`)
1. `POST /webhook` receives `{group_id, sender, text, timestamp}` from whatsapp-service
2. `append_message()` writes the message to the group's history file immediately
3. `process_message()` calls Gemini and gets a reply
4. Reply is POSTed to whatsapp-service `POST /send`

History is always saved regardless of whether the bot can respond (Gemini errors don't lose history).

## Gemini SDK
Uses `google-genai` package (NOT `google-generativeai` — the old SDK).
Import: `from google import genai`

**Critical constraint:** `gemini-2.5-flash` does NOT allow combining built-in tools (`google_search`) with function declarations in the same request. Choose one per call.

Current model: `gemini-2.5-flash` (free tier available).

## Gemini Tool Use Pattern (`gemini_client.py`)
Only the `get_group_history` function declaration is registered (no search grounding — incompatible).

**Two-phase call pattern:**
1. **First call:** send the user's message to Gemini with `get_group_history` tool available
2. Check the response parts for a `function_call` named `get_group_history`
3. **If tool called:** load the history file → make a second call with history injected as plain text context (no tools on second call)
4. **If no tool call:** use the first response directly

Gemini decides autonomously whether history is needed. "What did we decide last week?" → calls `get_group_history`. Simple questions → answers directly.

## Google Search
Currently disabled — incompatible with function declarations in gemini-2.5-flash.
To re-enable: either use search-only calls (no function declarations) or implement a custom `web_search` function declaration that calls an external search API (Brave, Tavily, etc.).

## History Files (`history_manager.py`)
- Stored in `group_histories/{sanitized_group_id}.txt`
- One line per message: `[2026-04-20 09:00] John: hey what's the plan?`
- Group ID is sanitized (special chars → `_`) for safe filenames
- **Write queue:** each group file has its own `asyncio.Lock` — prevents concurrent write corruption
- `read_history()` is synchronous (fast I/O, fine in async context)

## Cost Tracking (`cost_tracker.py`)
Called by `gemini_client.py` after every Gemini API response via `_track_cost()`.

**Pricing tiers** (gemini-2.5-flash):
- Tier 1 (≤128K total tokens per call): input $1.25/1M, output $5.00/1M
- Tier 2 (>128K total tokens per call): input $2.50/1M, output $10.00/1M

Tier determined by `input_tokens + output_tokens` for that call.

Each call appends one line to `cost_logs/YYYY-MM.txt`:
```
[2026-04-20 09:00] group=120363abc_g_us tier=1 in=1240 out=320 cost=$0.00210
```

`get_monthly_summary(year, month)` returns totals + per-group breakdown.

## Python Environment
Dependencies installed in `venv/`. Systemd service uses `venv/bin/uvicorn` directly.
To reinstall: `cd bot-service && venv/bin/pip install -r requirements.txt`
