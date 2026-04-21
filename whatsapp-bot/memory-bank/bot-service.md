# Bot Service

**Files:** `bot-service/main.py`, `gemini_client.py`, `history_manager.py`, `cost_tracker.py`, `policy_manager.py`, `reminders.py`, `timezone_manager.py`
**Runtime:** Python 3.12, FastAPI + Uvicorn, runs in a `venv/`

## Webhook Flow (`main.py`)
1. `POST /webhook` receives `{group_id, sender, sender_jid, text, timestamp, is_bot_mentioned, is_reply_to_bot}` from whatsapp-service
2. Assigns a per-group sequence number (policy 2 — last-message-only)
3. `append_message()` writes the user message to the group's history file immediately
4. Checks group policy (see Policy System below) — may skip processing
5. Checks reminder session (`_pending_reminder`) — handles yes/no confirmation and repeat flow
6. Checks slash commands (`/usage`, `/summarize`, `/reminders`) — handled directly, skip Gemini
7. `process_message()` calls Gemini and gets a reply
8. Checks sequence number again — skips reply if a newer message arrived (policy 2)
9. Handles special Gemini reply types: `set_reminder`, `update_timezone`, web search dict
10. Reply is POSTed to whatsapp-service `POST /send`
11. Bot reply is also saved to history via `append_message()` (sender = "Bot")

History is always saved regardless of whether the bot can respond (Gemini errors don't lose history).

## Policy System (`policy_manager.py`)

**Admin/control channel:** `Main` group (`120363428078252617@g.us`) — always active, never prompts for policy setup. All new-group management happens here.

**Per-group states** stored in `group_policies.json`:
- `new` — never seen this group; bot will notify Main and wait
- `pending` — notified Main, waiting for policy reply; bot ignores all messages from this group
- `active` — policy set, bot operates normally

**New group flow:**
1. Bot added to group → `groups.upsert` or `group-participants.update action=add` → POST `/group-joined` with group name
2. Bot sends to Main: *"I was invited to [group name]. What policy? 1=@mention only, 2=all messages, 3=listener only"*
3. User replies `1`, `2`, or `3` in Main → policy activated for that group

**Re-add flow (remove + re-add):**
Baileys does NOT reliably fire `group-participants.update` with `action=add` when the bot itself is re-added. Fix:
1. `action=remove` → `/group-left` → `reset_to_new()` + sends "⚠️ I was removed from *Name*" to Main
2. `groups.upsert` fires when bot rejoins → `/group-joined` → policy question sent to Main immediately
3. Fallback: if `groups.upsert` also misses, the next message from the group triggers the policy question

**Policy modes (active groups):**
- `mention_only=true` — reply only when @mentioned or replied to
- `mention_only=false` — reply to all messages
- `listener=true` — save messages to history but never reply; group is summarizable from Main via `/summarize`
- Policy 2 (always on): skip reply if a newer message arrived during Gemini processing

**`/group-joined` endpoint:** called by whatsapp-service when bot is added/rejoined. Idempotent — only acts if status is `new` (skips if already active or pending).

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
Four function declarations registered: `get_group_history`, `request_web_search`, `set_reminder`, `update_timezone`.

**Three-phase call pattern:**
1. **First call:** send the user's message with both function declarations available
2. Check response parts for a `function_call`:
   - `get_group_history` → load history file → second call with history injected as plain text (no tools)
   - `request_web_search` → store original message in `_pending_web_search[group_id]`, return a dict with buttons asking the user to confirm
3. **If no tool call:** use the first response directly

**Web search confirmation flow:**
- `process_message()` returns `{"text": "...", "buttons": [{"id": "web_search_yes", "text": "🔍 Yes, search"}, {"id": "web_search_no", "text": "❌ No thanks"}]}`
- `main.py` merges this dict into the `/send` payload → whatsapp-service renders as plain-text numbered list (native buttons are silently dropped by WhatsApp servers)
- User replies with the button ID text (`web_search_yes` / `web_search_no`) or a button tap if somehow received
- `process_message()` checks `_pending_web_search` and routes: `web_search_yes` → triggers `_web_search_call()`, `web_search_no` → dismisses

**`_web_search_call()`:** separate Gemini call using only `GoogleSearch` built-in tool (no function declarations — they conflict).

**`_pending_web_search`:** per-group dict `{group_id: original_user_message}` — cleared on any confirmation response or unrelated message.

**`set_reminder`** → returns `{"type": "set_reminder", "message", "iso_time", "repeat_interval"}` — handled in `main.py` session flow.

**`update_timezone`** → returns `{"type": "update_timezone", "timezone"}` — resolved via `resolve_timezone()` Gemini call, saved to `user_timezones.json` by JID.

**Local time context** is injected into every `process_message` call: `[Today is Monday 2026-04-21, current local time is 17:32 (Asia/Jerusalem)]` using the sender's timezone from `user_timezones.json`. This ensures Gemini resolves relative times ("in 5 minutes", "tonight") correctly in the user's local time, not UTC. The returned `iso_time` is then treated as naive local time in the setter's timezone.

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

## Reminders (`reminders.py`)
- **APScheduler** `AsyncIOScheduler` with `SQLAlchemyJobStore` → SQLite (`reminders.db`) — survives restarts
- `add_reminder(group_id, message, fire_at_utc, mention_jids, display_tz, repeat_interval)` → returns job ID
- `fire_reminder(group_id, message, mention_jids)` — async job function, calls whatsapp-service `/send` with `mention_jids`
- `list_reminders(group_id=None)` — None = all groups
- `cancel_reminder(short_id, allowed_group_id=None)` — validates group ownership unless `allowed_group_id=None`
- Repeat intervals: `"weekly"`, `"daily"`, `"every N minutes"`, `"monthly"`, `"yearly"` → mapped to `IntervalTrigger`
- Scheduler started/stopped in FastAPI lifespan

**Reminder session state** in `main.py` (`_pending_reminder[group_id]`):
- `awaiting_confirm` — bot asked "set reminder?", waiting for yes/no
- `awaiting_repeat` — reminder confirmed, bot asked about repeat, waiting for response
- `_awaiting_reply` is set so mention-only groups don't block the yes/no reply

## Timezones (`timezone_manager.py`)
- Stored in `user_timezones.json` keyed by participant **JID** (stable, unlike display names)
- Default: `Asia/Jerusalem` for all users
- `get_user_timezone(jid)`, `set_user_timezone(jid, tz)` — global across all groups
- `compute_reminder_jobs(participants, fire_at_naive, setter_jid)`:
  - Groups participants by timezone
  - Setter's timezone → group message (no @mention)
  - Other timezones → separate job with @mention at the same clock time in their timezone
- `resolve_timezone(text)` in `gemini_client.py` — Gemini converts "London" / "Tel Aviv" → IANA name

## Admin Commands
Handled in `main.py` before calling `process_message()`, so they bypass Gemini entirely.

| Command | Available in | Behaviour |
|---|---|---|
| `/summarize` | Any group | Summarizes today's messages in that group via Gemini. In Main: summarizes all active groups combined. |
| `/usage` | Main group only | Returns this month's Gemini call count, token usage, and cost, broken down per group. |
| `/reminders` | Any group | Lists pending reminders for that group. In Main: lists all groups. |
| `/reminders cancel #id` | Any group | Cancels reminder (own group only). Main can cancel any. |

## whatsapp-service endpoints (relevant to bot-service)
- `POST /send` — accepts `mention_jids: [jid]` for @mentions when firing reminders
- `GET /group-participants?group_id=` — returns `[{jid, name}]` for reminder timezone computation
- `GET /group-name?group_id=` — used for backfilling group names
- Webhook payload includes `sender_jid` (from `msg.key.participant`) alongside `sender` (display name)

## Python Environment
Dependencies installed in `venv/`. Systemd service uses `venv/bin/uvicorn` directly.
To reinstall: `cd bot-service && venv/bin/pip install -r requirements.txt`
Key packages: `fastapi`, `uvicorn`, `google-genai`, `httpx`, `apscheduler>=3.10`, `SQLAlchemy`, `tzdata`
