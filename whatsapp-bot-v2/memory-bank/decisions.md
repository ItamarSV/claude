# Design Decisions (v2)

## Three separate processes, not modules
Each block runs as its own process. Stronger isolation than module
boundaries — a crash in one block doesn't take the others down, and
each can be deployed/restarted independently.

## Logic owns the orchestration loop
The Gemini block does not execute tools and does not loop internally.
It returns a list of tool calls; Logic runs them one by one. If a tool
needs a follow-up Gemini call (e.g. `get_history`), Logic drives that
second call. This keeps the Gemini block stateless and replaceable.

## Scheduler stays inside Logic
Reminders fire from a sub-module inside the Logic block, not a fourth
service. APScheduler's SQLite jobstore already provides persistence;
splitting the scheduler into its own process would force it to HTTP
back into Logic for context (timezones, group policy) on every fire.

## `send_text` is a tool, not a separate response shape
Every Gemini response is a tool-call array. No special "plain reply"
field. This makes the contract uniform — one shape for everything.

## Tool list as an array (not single tool)
A single `/ask` call can return multiple tools to execute in order
(e.g. `[reminder.create, send_text]`). This avoids an extra round-trip
for the common "do something + tell the user it's done" pattern.

## Voice messages — file-based, not inline
WhatsApp block writes audio to disk and sends the path. Logic reads it,
sends to Gemini's `/transcribe`, then deletes. Cleaner than inline
base64 for 10-minute voice messages: smaller request bodies, readable
logs, easier to debug.

## Events on a separate endpoint
`POST /event` is distinct from `POST /webhook` so each contract stays
small. WhatsApp block knows the difference; Logic dispatches each.

## Static tool declarations inside Gemini block
The full tool schema lives in the Gemini block's system prompt — Logic
does not send tool definitions per call. Logic only needs to know
*which* tool names exist (so it can execute them).

## Naming
- Block names match the user's vocabulary: Logic / WhatsApp / Gemini.
- Service folders use the `<name>-service/` suffix.
- Folder lives at `/Users/itamarsvisa/Documents/Claude/whatsapp-bot-v2/`,
  sibling to the live repo (live code stays untouched during the
  redesign).
