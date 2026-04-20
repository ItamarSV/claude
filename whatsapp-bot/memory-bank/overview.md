# WhatsApp AI Bot — Overview

## Purpose
A personal WhatsApp bot that lives as its own WhatsApp user (registered on a Twilio virtual number). You invite it to any group you want. It answers questions using Gemini Pro with live Google Search, and can look back through a group's full chat history when needed.

## Architecture

```
WhatsApp Groups
      │  (Baileys — WhatsApp Web protocol)
      ▼
whatsapp-service  (Node.js, port 3000)
      │  HTTP POST /webhook
      ▼
bot-service       (Python/FastAPI, port 8000)
      │  google-generativeai SDK
      ▼
Gemini 1.5 Pro  ──► Google Search grounding (real-time info)
                └──► get_group_history tool (reads .txt file)
```

Both services run in Docker on a GCP VM, connected on a private Docker network.

## Key Decisions

| Decision | Choice | Why |
|---|---|---|
| WhatsApp library | Baileys (unofficial) | Meta Cloud API requires 100K conv/month for groups; max 8 participants |
| Two services | Node.js + Python | Baileys is Node.js only; Gemini tooling is best in Python |
| History storage | One .txt file per group | Gemini 1.5 Pro has 1M token context — entire history fits; no DB needed |
| History routing | Gemini tool use | Model decides when history is needed; no double API calls for simple questions |
| Cost tracking | Per-call log in monthly .txt | Auditable, readable, easy to parse for future monthly report |

## Known Limitations
- Baileys is unofficial — small risk of WhatsApp banning the number
- History files grow unboundedly (not a problem for years given 1M token window)
- Bot responds to every message in every group it's added to (no per-group policy yet)
- No support for images, voice, or other media — text only

## Future Ideas
- Monthly cost report sent automatically via WhatsApp
- Per-group bot policy (e.g., only respond when tagged)
- Admin commands (e.g., "!summary", "!costs")
- Switch to official Meta API if group support improves
