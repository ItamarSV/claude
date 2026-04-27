# WhatsApp AI Bot v2 — Overview

## Goal
A redesign of the WhatsApp AI bot using a clean three-block architecture.
Each block has one responsibility. Blocks communicate over HTTP using
locked JSON contracts (see `contracts.md`).

## The three blocks

```
              ┌──────────────────────────┐
              │      Logic block         │
              │   (brain + scheduler)    │
              └──────┬───────────────┬───┘
        text in/out  │               │  JSON in/out
                     │               │
        ┌────────────▼───┐    ┌──────▼─────────┐
        │ WhatsApp block │    │  Gemini block  │
        │  text in/out   │    │  JSON in/out   │
        └────────────────┘    └────────────────┘
                │                     │
            WhatsApp                Gemini
```

### Logic block — the brain
- Receives messages and events from the WhatsApp block
- Holds all business rules: when to reply, group policies, dialog sessions
- Owns the scheduler (sub-module) for reminders
- Calls the Gemini block for decisions or transcription
- Calls the WhatsApp block to send text
- Persists state on disk (policies, history, reminders, costs, timezones)

### WhatsApp block — text I/O only
- Receives messages from WhatsApp groups, forwards to Logic
- Forwards lifecycle events (bot added/removed) to Logic
- Sends text from Logic out to WhatsApp groups
- Knows nothing about Gemini, reminders, policies, or sessions

### Gemini block — JSON I/O only
- Stateless — receives a request, returns a response
- Has a static system prompt with all tool declarations baked in
- Two paths:
  - `/transcribe` — audio file → text
  - `/ask` — message + context → list of tool calls
- Logic supplies all context per call; Gemini block remembers nothing

## Folder layout

```
whatsapp-bot-v2/
├── memory-bank/        # living design documentation
├── logic-service/      # block B
├── whatsapp-service/   # block A
└── gemini-service/     # block C
```

## Status
Design phase. Contracts locked. No code written yet.
