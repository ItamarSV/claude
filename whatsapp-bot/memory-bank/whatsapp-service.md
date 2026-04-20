# WhatsApp Service

**File:** `whatsapp-service/index.js`
**Runtime:** Node.js 20, CommonJS (not ESM — Baileys is a CJS package)

## What Baileys Is
Baileys implements the WhatsApp Web multi-device protocol in Node.js. It connects to WhatsApp's servers the same way WhatsApp Web does — no official API involved. The bot's number must be registered as a real WhatsApp account first (one-time QR scan).

## Important: CommonJS Required
Baileys (`@whiskeysockets/baileys`) is a CommonJS package. The service must use `require()` not `import`. Using `"type": "module"` in package.json or ESM `import` statements will cause `makeWASocket is not a function` at runtime.

## QR Code Scanning
- On first run (no `.baileys_auth/` session): a QR code is generated
- Access `http://VM_IP:3000/qr` in a browser to see the QR code as an image
- Scan with the bot's WhatsApp account (the eSIM number)
- The page auto-refreshes every 20 seconds (QR codes expire)
- Session is saved to `.baileys_auth/` — survives restarts

## Session Management
- Auth state is stored in `.baileys_auth/` (gitignored, Docker volume or local dir)
- After scanning, the session persists indefinitely in `.baileys_auth/`
- If session expires (WhatsApp revoked it): delete `.baileys_auth/`, restart service, re-scan QR at `/qr`

## Message Filtering
On every incoming message the service checks:
1. `type !== 'notify'` → skip (historical/status messages)
2. `!jid.endsWith('@g.us')` → skip (not a group message)
3. `msg.key.fromMe === true` → skip (bot's own message — prevents reply loops)
4. No text content (`conversation` or `extendedTextMessage.text`) → skip (images, stickers, etc.)

Only messages passing all checks are forwarded to bot-service.

## HTTP Interface

### `GET /qr`
Serves an HTML page with the WhatsApp QR code as an image. Use this for initial setup.
Auto-refreshes every 20 seconds. Returns a message if already connected.

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

## Node.js Version
Requires Node.js 20+. Node 18 causes Baileys engine check to fail during `npm install`.
Install via: `curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs`
