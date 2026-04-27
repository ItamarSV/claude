# Inter-Block Contracts (LOCKED)

All blocks talk over HTTP/JSON. Five endpoints total.

## Map

```
WhatsApp ──▶ Logic    POST /webhook    (inbound message)
WhatsApp ──▶ Logic    POST /event      (lifecycle event)

Logic ──▶ WhatsApp    POST /send       (outbound message)

Logic ──▶ Gemini      POST /transcribe (audio → text)
Logic ──▶ Gemini      POST /ask        (message → tool calls)
```

---

## 1. WhatsApp → Logic — `POST /webhook`

Inbound message from a WhatsApp group.

```json
{
  "message_id":   "string",
  "group_id":     "string",
  "sender_id":    "string",
  "text":         "string | null",
  "audio_path":   "string | null",
  "is_reply_to":  "message_id | null",
  "timestamp":    "iso8601"
}
```

Exactly one of `text` or `audio_path` is present.

`audio_path` is an absolute path on the shared filesystem (e.g.
`/tmp/voice/<message_id>.ogg`). Logic owns deletion after transcription.

`is_reply_to` lets Logic know when a message is a reply to a previous message
(used to detect replies to the bot itself).

---

## 2. WhatsApp → Logic — `POST /event`

Lifecycle event (not a message).

```json
{
  "type":      "group_added | group_removed",
  "group_id":  "string",
  "timestamp": "iso8601",
  "data":      { /* event-specific */ }
}
```

### Event types (v2)

| `type` | `data` shape |
|---|---|
| `group_added` | `{ "group_name": "string" }` |
| `group_removed` | `{}` |

Logic decides what to do per event (e.g. ask for policy in Main on
`group_added`).

---

## 3. Logic → WhatsApp — `POST /send`

Send text to a group.

```json
{
  "group_id":  "string",
  "text":      "string",
  "reply_to":  "message_id | null"
}
```

`reply_to` makes the outgoing message a quoted reply to a specific message.

---

## 4. Logic → Gemini — `POST /transcribe`

Audio → text.

Request:
```json
{ "audio_path": "string" }
```

Response:
```json
{ "text": "string" }
```

Audio is read from disk by the Gemini block. Logic deletes the file after
the call returns (success or failure).

---

## 5. Logic → Gemini — `POST /ask`

Message + context → list of tool calls to execute in order.

Request:
```json
{
  "message": "string",
  "context": { /* anything Logic chooses to include */ }
}
```

Response:
```json
{
  "tools": [
    { "name": "string", "args": { /* per tool */ } }
  ]
}
```

The response is **always an array of tool calls**, in execution order.

- Plain reply → `[{ "name": "send_text", "args": { "text": "..." } }]`
- Action + confirmation → `[{ "name": "reminder", "args": {...} }, { "name": "send_text", "args": {...} }]`
- Need data first → `[{ "name": "get_history", "args": {...} }]`
  (Logic runs it, then calls `/ask` again with the result in `context`)

The `context` object is unstructured by contract — Logic and the Gemini
block agree on what to include (recent history, current time, user
timezone, reminders snapshot, voice indicator, etc.).
