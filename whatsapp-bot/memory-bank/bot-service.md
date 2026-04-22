# Bot Service

**Files:** `bot-service/main.py`, `gemini_client.py`, `history_manager.py`, `cost_tracker.py`, `policy_manager.py`, `reminders.py`, `timezone_manager.py`
**Runtime:** Python 3.12, FastAPI + Uvicorn, runs in a `venv/`

## Webhook Flow (`main.py`)
1. `POST /webhook` receives `{group_id, sender, sender_jid, text, timestamp, is_bot_mentioned, is_reply_to_bot, audio_data?, audio_mime?}`
2. Assigns a per-group sequence number (last-message-only policy)
3. **If `audio_data` present and no text:** `transcribe_audio()` → `msg.text = "[voice] <transcription>"`
4. `append_message()` writes to history immediately
5. **Policy checks** — skip listener groups; mention-only filter (session owners bypass it)
6. **Session routing** (see Dialog Session Manager below) — runs before slash commands
7. **Slash commands** (`/usage`, `/summarize`, `/reminders`) — handled directly, skip Gemini
8. Fetches pending reminders → formats as context string for Gemini
9. `process_message()` calls Gemini
10. Checks sequence number — skips if newer message arrived
11. Handles structured Gemini results: `web_search` → open session; `set_reminder` → schedule directly; `update_timezone` → save; `cancel_reminder` → direct cancel
12. Reply POSTed to `/send`, saved to history

History is always saved regardless of whether the bot can respond (Gemini errors don't lose history).

## Dialog Session Manager (`session_manager.py`)

Sessions are background tasks the bot holds open until the user provides a required piece of information (approval, repeat interval, etc.).

**Key rules:**
- Session key is `(group_id, user_jid)` — strict group isolation, per-user within a group
- Multiple users in the same group can have independent sessions simultaneously
- One active session per user at a time — if user triggers a second, bot says finish the first
- Session owners bypass mention-only filter for their replies
- Slash commands always bypass session routing
- Per-(group, user) `asyncio.Lock` prevents race conditions on concurrent messages

**Session types:**
| Type | Opened when | Waits for | On proceed |
|---|---|---|---|
| `web_search` | Gemini calls `request_web_search` | User approves internet search | `web_search_call()` |
| `reminder_repeat` | `set_reminder` returns `repeat_interval="ask"` | User specifies repeat interval | Re-schedule with CronTrigger/IntervalTrigger |

**Lifecycle:**
1. **Open** — `_open_session()` stores session, starts 5-min `asyncio` timeout task
2. **Message from session owner** → single `handle_session_message()` Gemini call:
   - `proceed` → `_execute_session()`, close session
   - `cancel` → close session, send farewell
   - `ignore` → answer their question normally, session stays open silently (no re-asking)
3. **Message from other user** → processed normally, zero session interference
4. **Timeout (5 min)** → `generate_timeout_message()` sends natural @mention closure, session moved to ghost

**Ghost sessions (2-min window after timeout):**
If the original user sends an approval within 2 minutes of the timeout message, the session is revived automatically and the action executes immediately. After 2 minutes the ghost expires — user must trigger the action again explicitly.

**`DialogSession` fields:** `session_id`, `group_id`, `user_jid`, `user_name`, `type`, `question`, `data` (type-specific payload), `created_at`, `timeout_task`

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
Five function declarations registered: `get_group_history`, `request_web_search`, `set_reminder`, `update_timezone`, `cancel_reminder`.

**Call pattern:**
1. **First call:** send user message with all function declarations
2. Check response for a `function_call`:
   - `get_group_history` → load history → second call with history injected (no tools)
   - `request_web_search` → returns `{"type": "web_search", "question": "...", "original_message": "..."}` — `main.py` opens a dialog session
   - `set_reminder` → returns `{"type": "set_reminder", "message", "iso_time", "repeat_interval", "confirmation_message"}` — `main.py` schedules directly and sends `confirmation_message`
   - `update_timezone` → returns `{"type": "update_timezone", "timezone"}` — resolved and saved
   - `cancel_reminder` → returns `{"type": "cancel_reminder", "reminder_id"}` — direct cancel, no session
3. **If no tool call:** use the first response directly

**`request_web_search`** includes a `question` field — Gemini writes a contextual approval question (e.g. *"The Champions League final was yesterday — want me to look up the result?"*). No hardcoded text, no buttons.

**`set_reminder`** includes `confirmation_message` — Gemini writes the natural confirmation (e.g. *"Done! I'll remind you to call mom tonight at 8pm."*). Reminder is set directly without a confirmation session. A `reminder_repeat` session is only opened when `repeat_interval="ask"`.

**`web_search_call(group_id, user_message) -> str`:** separate Gemini call using only `GoogleSearch` built-in tool (no function declarations — they conflict). Called by `_execute_session` when web_search session proceeds.

**Local time context** is injected into every `process_message` call: `[Today is Monday 2026-04-21, current local time is 17:32 (Asia/Jerusalem)]`.

**Pending reminders context** is injected into every `process_message` call so Gemini can cancel the right reminder by description.

## Gemini Utility Functions (`gemini_client.py`)

**`handle_session_message(session_type, session_question, session_data, user_text, recent_history) -> dict`**
Single Gemini call for active dialog sessions. Returns `{"action": "proceed"|"cancel"|"ignore", "reply": str, "interval"?: str}`.
- `proceed` — user approved; for `reminder_repeat`, also returns `interval`
- `cancel` — user declined or wants to move on
- `ignore` — unrelated message; `reply` is the normal answer to their question; session stays open silently

**`generate_timeout_message(session_type, session_data, user_name) -> str`**
Generates a natural @mention timeout message, e.g. *"@David I didn't get your approval, so I won't search the NBA results."*

**`transcribe_audio(group_id, audio_data_b64, audio_mime) -> str`**
Sends audio as inline bytes to Gemini. Returns spoken words. Cost tracked.

**`resolve_repeat_interval(text) -> dict | None`**
Converts natural language repeat schedule to APScheduler trigger dict.

**`resolve_timezone(text) -> str`**
Converts city/region name to IANA timezone.

**`summarize_text(group_id, prompt) -> str`**
Generic Gemini call for `/summarize` output.

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
- `add_reminder(group_id, message, fire_at_utc, mention_jids, display_tz, repeat_interval, repeat_spec)` → returns job ID
- `fire_reminder(group_id, message, mention_jids, repeat_interval)` — async job function; `repeat_interval` stored in kwargs so it can be read back for display
- `list_reminders(group_id=None)` — None = all groups; returns list with `repeat_interval` field (reads from kwargs if stored, falls back to `_trigger_to_interval`)
- `cancel_reminder(short_id, allowed_group_id=None)` — validates group ownership unless `allowed_group_id=None` (Main can pass `None` to cancel any group's reminder)
- Scheduler started/stopped in FastAPI lifespan

**Trigger building (`_build_trigger`):** prefers `repeat_spec` dict (from Gemini), falls back to `repeat_interval` string, then `DateTrigger` for one-time:
- `repeat_spec = {"type": "cron", "day_of_week": "mon,sun"}` → `CronTrigger` (supports day-of-week patterns like "every Monday and Sunday")
- `repeat_spec = {"type": "interval", "weeks": 1}` → `IntervalTrigger`
- `repeat_interval` string ("weekly", "daily", "every 30 minutes") → `IntervalTrigger` via `_repeat_trigger()`

**`_trigger_to_interval(trigger)`:** reads an `IntervalTrigger`'s timedelta and returns a human string ("daily", "weekly", "monthly", "yearly", "every N hours/minutes"). Used to display repeat info for reminders created before `repeat_interval` was stored in kwargs.

**Access control (enforced in `main.py`):**
- **Main group**: can list all reminders across all groups, cancel any reminder (`allowed_group_id=None`)
- **Other groups**: can only list their own reminders, can only cancel their own reminders (`allowed_group_id=group_id`)

**Reminder flow (no confirmation session):**
- When Gemini calls `set_reminder` with a valid `iso_time`, the reminder is scheduled immediately. `confirmation_message` from Gemini is sent as the reply.
- If `repeat_interval="ask"`, a one-time job is scheduled and a `reminder_repeat` dialog session opens asking how often.
- No `_pending_reminder` or `_awaiting_reply` state — replaced by the session manager.

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
| `/reminders` | Any group | Lists pending reminders for that group (with repeat interval if set). In Main: lists all groups. |
| `/reminders cancel #id` | Any group | Cancels reminder (own group only). Main can cancel any reminder. |

## whatsapp-service endpoints (relevant to bot-service)
- `POST /send` — accepts `mention_jids: [jid]` for @mentions when firing reminders
- `GET /group-participants?group_id=` — returns `[{jid, name}]` for reminder timezone computation
- `GET /group-name?group_id=` — used for backfilling group names
- Webhook payload includes `sender_jid` (from `msg.key.participant`) alongside `sender` (display name)

## Python Environment
Dependencies installed in `venv/`. Systemd service uses `venv/bin/uvicorn` directly.
To reinstall: `cd bot-service && venv/bin/pip install -r requirements.txt`
Key packages: `fastapi`, `uvicorn`, `google-genai`, `httpx`, `apscheduler>=3.10`, `SQLAlchemy`, `tzdata`
