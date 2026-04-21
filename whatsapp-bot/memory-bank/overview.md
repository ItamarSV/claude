# WhatsApp AI Bot — Overview

## Purpose
A personal WhatsApp bot that lives as its own WhatsApp Business user (real eSIM, +972559925787). You invite it to any group you want. It answers questions using Gemini with live Google Search, sets reminders, and can look back through a group's full chat history when needed.

## Architecture

```
WhatsApp Groups
      │  (Baileys — WhatsApp Web protocol)
      ▼
whatsapp-service  (Node.js, port 3000)
      │  HTTP POST /webhook
      ▼
bot-service       (Python/FastAPI, port 8000)
      │  google-genai SDK
      ▼
gemini-2.5-flash  ──► GoogleSearch built-in tool (real-time info, separate call)
                  ├──► get_group_history function declaration (reads .txt file)
                  ├──► request_web_search function declaration (triggers search confirmation)
                  ├──► set_reminder function declaration (schedules via APScheduler)
                  └──► update_timezone function declaration (updates user_timezones.json)
```

Both services run as systemd units on a GCP VM (no Docker).

## Key Decisions

| Decision | Choice | Why |
|---|---|---|
| WhatsApp library | Baileys (unofficial) | Meta Cloud API requires 100K conv/month for groups; max 8 participants |
| Two services | Node.js + Python | Baileys is Node.js only; Gemini tooling is best in Python |
| History storage | One .txt file per group | gemini-2.5-flash has large context — entire history fits; no DB needed |
| History routing | Gemini tool use | Model decides when history is needed; no double API calls for simple questions |
| Cost tracking | Per-call log in monthly .txt | Auditable, readable, easy to parse for future monthly report |

## Admin Commands
| Command | Where | What it does |
|---|---|---|
| `/summarize` | Any group | Summarizes today's conversation; in Main summarizes all groups |
| `/usage` | Main only | This month's Gemini call count, tokens, and cost per group |
| `/reminders` | Any group | Lists pending reminders for that group; in Main lists all groups |
| `/reminders cancel #id` | Any group | Cancels a reminder (own group only; Main can cancel any) |

**Natural language commands (via Gemini):**
- `@bot remind me at 18:00 to call David` — sets a reminder with optional repeat
- `@bot set my timezone to London` — updates the user's timezone across all groups

## Group Policies
| Option | Mode | Behaviour |
|---|---|---|
| `1` | @mention only | Replies when explicitly @mentioned or replied to |
| `2` | All messages | Replies to every message |
| `3` | Listener | Reads silently, never replies — summarizable from Main via `/summarize` |

Policy is set from Main when bot is added to a group. Remove + re-add resets the policy.

## Group Lifecycle Events
| Event | Trigger | What happens |
|---|---|---|
| Bot added (first time) | `group-participants.update action=add` or `groups.upsert` | Notify Main with policy question |
| Bot removed | `group-participants.update action=remove` | Notify Main "⚠️ I was removed from *Name*", reset group to "new" |
| Bot re-added | `groups.upsert` (primary), or first message in group (fallback) | Notify Main with policy question |

Note: `action=add` does NOT reliably fire when the bot is re-added to a group it was previously in. `groups.upsert` is used as the primary trigger.

## Known Limitations
- Baileys is unofficial — small risk of WhatsApp banning the number
- History files grow unboundedly (not a problem for years given 1M token window)
- No support for images, voice, or other media — text only
- Interactive buttons silently dropped by WhatsApp servers — using plain-text numbered list (1/2) instead

## Future Ideas
- Monthly cost report sent automatically via WhatsApp
- Switch to official Meta API if group support improves
