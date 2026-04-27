# Tool Catalogue (LOCKED for v2)

The Gemini block's static system prompt declares these tools. Every
response from `/ask` is a list of these tool calls.

## `send_text`

Send a plain text message back to the group.

```json
{ "name": "send_text", "args": { "text": "string" } }
```

Every reply path ends with `send_text`. There is no separate "plain reply"
mechanism — Gemini always emits a tool call.

## `reminder`

Manage reminders. One tool, three actions.

### Create
```json
{
  "name": "reminder",
  "args": {
    "action":  "create",
    "message": "string",
    "iso_time": "iso8601",
    "repeat":  "none | daily | weekly | cron:<expr>"
  }
}
```

### Update
```json
{
  "name": "reminder",
  "args": {
    "action": "update",
    "id":     "string",
    "fields": {
      "message":  "string?",
      "iso_time": "iso8601?",
      "repeat":   "string?"
    }
  }
}
```

### Delete
```json
{
  "name": "reminder",
  "args": {
    "action": "delete",
    "id":     "string"
  }
}
```

## `get_history`

Ask Logic for older messages from this group's history. Logic runs it and
calls `/ask` again with the history in `context`.

```json
{
  "name": "get_history",
  "args": {
    "limit": 50,
    "since": "iso8601"
  }
}
```

`limit` and `since` are both optional. Empty args = full history.

## `web_search`

Trigger a web search. Logic decides whether to confirm with the user first
(via `send_text` ahead of it) or run it directly.

```json
{
  "name": "web_search",
  "args": { "query": "string" }
}
```
