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
- Scan with the bot's **WhatsApp Business** app (the eSIM number +972559925787)
- The page auto-refreshes every 20 seconds (QR codes expire)
- Session is saved to `.baileys_auth/` — survives restarts

**Note:** The bot number is registered as WhatsApp Business. Baileys works with both regular and Business accounts — no difference in protocol.

## Session Management
- Auth state is stored in `.baileys_auth/` (gitignored, Docker volume or local dir)
- After scanning, the session persists indefinitely in `.baileys_auth/`
- If session expires (WhatsApp revoked it): delete `.baileys_auth/`, restart service, re-scan QR at `/qr`

## Message Filtering
On every incoming message the service checks:
1. `type !== 'notify'` → skip (historical/status messages)
2. `!jid.endsWith('@g.us')` → skip (not a group message)
3. `msg.key.fromMe === true` → skip (bot's own message — prevents reply loops)
4. No text content and no button response → skip (images, stickers, etc.)

Text is extracted from: `conversation`, `extendedTextMessage.text`, or button clicks (`interactiveResponseMessage.nativeFlowResponseMessage.paramsJson` → `id` field).

Every forwarded message includes `is_bot_mentioned: bool` (true if bot's JID appears in `extendedTextMessage.contextInfo.mentionedJid`). Filtering by mention is handled in bot-service based on per-group policy, not here.

Bot's own JID is captured on `connection === 'open'`:
- `botNumber` from `sock.user.id` (e.g. `972559925787`)
- `botLid` from `state.creds.me.lid` (e.g. `36014072553559`) — `sock.user.lid` is null for WhatsApp Business accounts; the LID lives in `state.creds.me.lid`

Both are checked when detecting mentions/participants. @mentions in WhatsApp Business arrive as `@lid` format in `mentionedJid`.

**@mention text stripping:** WhatsApp Business embeds raw LIDs in message text (e.g. `@36014072553559 hello`). Before forwarding to bot-service, the text is cleaned with `text.replace(/@\d+/g, '').trim()` so Gemini never sees the raw ID.

## HTTP Interface

### `GET /qr`
Serves an HTML page with the WhatsApp QR code as an image. Use this for initial setup.
Auto-refreshes every 20 seconds. Returns a message if already connected.

### `POST /send`
Called by bot-service to send a reply to a group.
```json
{ "group_id": "120363abc@g.us", "text": "The meeting is at 3pm." }
```
Optional `buttons` array sends a numbered plain-text choice instead of plain text:
```json
{
  "group_id": "120363abc@g.us",
  "text": "Want me to search the web?",
  "buttons": [
    { "id": "web_search_yes", "text": "🔍 Yes, search" },
    { "id": "web_search_no", "text": "❌ No thanks" }
  ]
}
```
Rendered as: `<text>\n\n1. 🔍 Yes, search\n2. ❌ No thanks\n\nReply *1* or *2*`

**Note:** `nativeFlowMessage` via `proto`/`relayMessage` was tried and silently dropped by WhatsApp (no error thrown, message never arrived). Plain-text numbered list is the reliable approach for personal accounts.

### `POST /group-joined` (bot-service endpoint, called by whatsapp-service)
Triggered via `group-participants.update` when bot is added to a group. Fetches group metadata to get the name, then POSTs `{group_id, group_name}` to bot-service `/group-joined`.

### `GET /health`
Returns `{ "ok": true }`. Used to verify the service is alive.

## Reconnection
On connection close, checks the disconnect reason code:
- If `loggedOut` (code 401): does NOT reconnect — session is invalidated, manual re-scan needed
- Any other code: waits 5 seconds, then calls `connectToWhatsApp()` again

## Node.js Version
Requires Node.js 20+. Node 18 causes Baileys engine check to fail during `npm install`.
Install via: `curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs`
