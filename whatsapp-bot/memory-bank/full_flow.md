# WhatsApp AI Bot — Complete Message Flow

## From user message to final answer

---

**1. Message arrives**
User sends a message in a WhatsApp group. Baileys (Node.js) receives it, extracts text/audio, and POSTs it to bot-service `/webhook`.

---

**2. Pre-processing (main.py)**
- Assign sequence number (for last-message-only policy)
- Transcribe audio if needed
- Save message to history file
- Policy checks (is bot allowed to reply in this group? is it @mentioned?)
- Session routing — if this user has an open dialog session, handle it separately (skip to step 6)

---

**3. Build the Gemini request**
`process_message()` assembles the full prompt:
```
[Today is Wednesday 2026-04-22, current time is 14:30 (Asia/Jerusalem)]

Pending reminders:
#af4e6a | Thu Apr 23 07:00 — דוח

Recent conversation (last 2 hours):
[14:28] Itamar: מה השעה?
[14:29] Bot: 14:29.

New message:
Itamar: remind me to call mom at 8pm
```
Plus the 5 function declarations (the tool schemas).

---

**4. First Gemini call**
Gemini reads the message + declarations and responds with **one of two things**:

**A) Plain text** → flow jumps to step 7.

**B) A function call:**
```json
{
  "name": "set_reminder",
  "args": {
    "message": "call mom",
    "iso_time": "2026-04-22T20:00:00",
    "confirmation_message": "סבבה! אזכיר לך להתקשר לאמא הערב ב-8."
  }
}
```

---

**5. Tool dispatch (main.py)**
Your code reads `fc.name`, looks it up in the tool registry, calls the handler with `args + context`.

Each tool does its work:
- `get_group_history` → loads history file → **second Gemini call** with full history → returns text answer
- `request_web_search` → opens a dialog session → sends approval question to group
- `set_reminder` → schedules the job → returns `confirmation_message` from args
- `cancel_reminder` → cancels the job → returns `cancellation_message` from args
- `update_timezone` → saves timezone → returns `confirmation_message` from args

---

**6. Dialog session (if open)**
If the user had an active session (e.g. waiting for repeat interval), `handle_session_message()` is called instead of `process_message()`. Gemini classifies the reply as `proceed / cancel / ignore` using a response schema (guaranteed JSON). Then `_execute_session()` does the work.

---

**7. Send reply**
- Sequence check — if a newer message arrived while Gemini was processing, discard this reply
- `POST /send` → whatsapp-service → Baileys sends message to group
- Save bot reply to history file

---

## The key guarantee: what Gemini returns

| Gemini returns | Your code does |
|---|---|
| Plain text | Send it directly |
| `function_call` with `name` + `args` | Dispatch to tool handler |
| Tool handler returns `reply` | Send it |
| Tool handler returns `session` | Open dialog, send question |

Gemini never sends to WhatsApp itself. It only decides *what* to do — your code does *everything* else.

---

## Tool definition structure

Each tool has two layers:

**Gemini declaration** — what Gemini sees and fills in:
- `name` — identifier your code checks
- `description` — prompt engineering; tells Gemini *when* to call this tool
- `parameters` — schema of args Gemini must fill in (`required` + optional)

**Handler contract** — what your code owns:
- `context` (MessageContext) — built by main.py, passed to every handler: `group_id`, `group_name`, `sender_jid`, `sender_name`, `text`
- `output` (ToolResult) — what the handler returns to main.py: `reply: str | None`, `session: DialogSession | None`

## Registered tools

| Tool | Triggered when | Handler output |
|---|---|---|
| `get_group_history` | Question about something >2h ago | Second Gemini call → plain text |
| `request_web_search` | Real-time info needed | Opens `web_search` dialog session |
| `set_reminder` | User asks to be reminded | Schedules job; optionally opens `reminder_repeat` session |
| `cancel_reminder` | User asks to cancel a reminder | Cancels job; sends confirmation |
| `update_timezone` | User asks to change timezone | Saves timezone; sends confirmation |
