# WhatsApp Service

**File:** `whatsapp-service/index.js`
**Runtime:** Node.js 20, ESM modules

## What Baileys Is
Baileys implements the WhatsApp Web multi-device protocol in Node.js. It connects to WhatsApp's servers the same way WhatsApp Web does — no official API involved. The bot's number must be registered as a real WhatsApp account first (one-time QR scan).

## Session Management
- Auth state is stored in `.baileys_auth/` (gitignored, volume-mounted in Docker)
- On first run: QR code is printed to the terminal — scan it once with the bot's WhatsApp account
- After scanning, the session persists indefinitely in `.baileys_auth/`
- If session expires (WhatsApp revoked it): delete `.baileys_auth/`, restart, scan again

## Message Filtering
On every incoming message the service checks:
1. `type !== 'notify'` → skip (historical/status messages)
2. `!jid.endsWith('@g.us')` → skip (not a group message)
3. `msg.key.fromMe === true` → skip (bot's own message — prevents reply loops)
4. No text content (`conversation` or `extendedTextMessage.text`) → skip (images, stickers, etc.)

Only messages passing all checks are forwarded to bot-service.

## HTTP Interface

### `POST /send`
Called by bot-service to send a reply to a group.
```json
{ "group_id": "120363abc@g.us", "text": "The meeting is at 3pm." }
```
Returns `{ "ok": true }` on success.

### `GET /health`
Returns `{ "ok": true }`. Used to verify the service is alive.

## Reconnection
On connection close, checks the disconnect reason code:
- If `loggedOut` (code 401): does NOT reconnect — session is invalidated, manual re-scan needed
- Any other code: waits 5 seconds, then calls `connectToWhatsApp()` again
