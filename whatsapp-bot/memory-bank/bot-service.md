# Bot Service

**Files:** `bot-service/main.py`, `gemini_client.py`, `history_manager.py`, `cost_tracker.py`, `policy_manager.py`
**Runtime:** Python 3.12, FastAPI + Uvicorn, runs in a `venv/`

## Webhook Flow (`main.py`)
1. `POST /webhook` receives `{group_id, sender, text, timestamp, is_bot_mentioned, is_reply_to_bot}` from whatsapp-service
2. Assigns a per-group sequence number (policy 2 — last-message-only)
3. `append_message()` writes the user message to the group's history file immediately
4. Checks group policy (see Policy System below) — may skip processing
5. Checks for admin commands (`/summarize`, `/usage`) — handled directly, skip Gemini
6. `process_message()` calls Gemini and gets a reply
7. Checks sequence number again — skips reply if a newer message arrived (policy 2)
8. Reply is POSTed to whatsapp-service `POST /send`
9. Bot reply is also saved to history via `append_message()` (sender = "Bot")

History is always saved regardless of whether the bot can respond (Gemini errors don't lose history).

## Policy System (`policy_manager.py`)

**Admin/control channel:** `Main` group (`120363428078252617@g.us`) — always active, never prompts for policy setup. All new-group management happens here.

**Per-group states** stored in `group_policies.json`:
- `new` — never seen this group; bot will notify Main and wait
- `pending` — notified Main, waiting for policy reply; bot ignores all messages from this group
- `active` — policy set, bot operates normally

**New group flow:**
1. Bot added to group → `group-participants.update` → POST `/group-joined` with group name
2. Bot sends to Main: *"I was invited to [group name]. What policy? 1=@mention only, 2=all messages"*
3. User replies `1` or `2` in Main → policy activated for that group

**Policy enforcement (active groups):**
- Policy 1 (`mention_only`): skip if `is_bot_mentioned` is false
- Policy 2 (always on): skip reply if a newer message arrived during Gemini processing

**`/group-joined` endpoint:** called by whatsapp-service when bot is added to a group. Idempotent — only acts if status is `new`.

**`group_policies.json` structure:**
```json
{
  "_pending": {"group_id": "120363xyz@g.us", "group_name": "Cooking Club"},
  "120363abc@g.us": {"status": "active", "mention_only": true, "name": "Cooking Club"}
}
```
Group `name` is saved on activation (from the `_pending` entry) and used in `/summarize` and `/usage` output instead of raw group IDs.

**`get_all_active_groups()`** returns `[(group_id, name)]` for all active non-main groups — used by `/summarize` to iterate all groups.

## Gemini SDK
Uses `google-genai` package (NOT `google-generativeai` — the old SDK).
Import: `from google import genai`

**Critical constraint:** `gemini-2.5-flash` does NOT allow combining built-in tools (`google_search`) with function declarations in the same request. Choose one per call.

Current model: `gemini-2.5-flash` (free tier available).

## Gemini Tool Use Pattern (`gemini_client.py`)
Two function declarations registered: `get_group_history` and `request_web_search`.

**Three-phase call pattern:**
1. **First call:** send the user's message with both function declarations available
2. Check response parts for a `function_call`:
   - `get_group_history` → load history file → second call with history injected as plain text (no tools)
   - `request_web_search` → store original message in `_pending_web_search[group_id]`, return a dict with buttons asking the user to confirm
3. **If no tool call:** use the first response directly

**Web search confirmation flow:**
- `process_message()` returns `{"text": "...", "buttons": [{"id": "web_search_yes", "text": "🔍 Yes, search"}, {"id": "web_search_no", "text": "❌ No thanks"}]}`
- `main.py` merges this dict into the `/send` payload → whatsapp-service sends interactive buttons
- User clicks a button → Baileys extracts the button ID and forwards it as text to bot-service
- `process_message()` checks `_pending_web_search` and routes: `web_search_yes` → triggers `_web_search_call()`, `web_search_no` → dismisses

**`_web_search_call()`:** separate Gemini call using only `GoogleSearch` built-in tool (no function declarations — they conflict).

**`_pending_web_search`:** per-group dict `{group_id: original_user_message}` — cleared on any confirmation response or unrelated message.

Gemini decides autonomously whether history or search is needed.

## History Files (`history_manager.py`)
- Stored in `group_histories/{sanitized_group_id}.txt`
- One line per message: `[2026-04-20 09:00] John: hey what's the plan?`
- Bot replies also saved: `[2026-04-20 09:01] Bot: The meeting is at 3pm.`
- Group ID is sanitized (special chars → `_`) for safe filenames
- **Write queue:** each group file has its own `asyncio.Lock` — prevents concurrent write corruption
- `read_history()` — full history (used by `get_group_history` tool)
- `read_recent_history(group_id, hours=2)` — last N hours (prepended as context to every Gemini call)
- `read_history_since(group_id, since: datetime)` — since a given datetime (used by `/summarize`)

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

`get_monthly_summary(year, month)` returns totals + per-group breakdown (`calls`, `tokens`, `cost`).

## Admin Commands
Handled in `main.py` before calling `process_message()`, so they bypass Gemini entirely.

| Command | Available in | Behaviour |
|---|---|---|
| `/summarize` | Any group | Summarizes today's messages in that group via Gemini. In Main: summarizes all active groups combined. |
| `/usage` | Main group only | Returns this month's Gemini call count, token usage, and cost, broken down per group. |

## Python Environment
Dependencies installed in `venv/`. Systemd service uses `venv/bin/uvicorn` directly.
To reinstall: `cd bot-service && venv/bin/pip install -r requirements.txt`
