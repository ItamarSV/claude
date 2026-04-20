# Operations Guide

## First-Time Setup

### 1. Get a Twilio virtual number
- Go to twilio.com → buy a local phone number (~$1/month)
- Make sure it can receive SMS (needed for WhatsApp verification)

### 2. Register the number on WhatsApp
- On your phone: open WhatsApp → add account → enter the Twilio number
- WhatsApp will send a verification SMS → check Twilio Console → Message Logs for the code
- Complete registration — you now have a second WhatsApp account

### 3. Deploy on GCP VM
```bash
git clone https://github.com/ItamarSV/claude.git
cd claude/whatsapp-bot
cp .env.example .env
# Edit .env and fill in your GEMINI_API_KEY
nano .env

docker-compose up -d
```

### 4. Scan the QR code (one-time)
```bash
docker-compose logs -f whatsapp-service
# Wait for the QR code to appear in the terminal
# On your phone: open the bot's WhatsApp account → Linked Devices → Link a Device → scan QR
```

The session is saved in `whatsapp-service/.baileys_auth/`. You won't need to scan again unless WhatsApp revokes the session.

### 5. Add the bot to a group
Open WhatsApp on your main number → open any group → Add Participant → add the Twilio number.

---

## Environment Variables

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` | From Google AI Studio — required |

All other configuration (ports, service URLs) is set directly in `docker-compose.yml`.

---

## Day-to-Day Commands

```bash
# Start everything
docker-compose up -d

# Stop everything
docker-compose down

# View logs (live)
docker-compose logs -f

# View logs for one service
docker-compose logs -f whatsapp-service
docker-compose logs -f bot-service

# Restart one service (e.g. after code change)
docker-compose restart bot-service

# Rebuild after code changes
docker-compose up -d --build bot-service
```

---

## Checking Health

```bash
# Is whatsapp-service up?
curl http://localhost:3000/health

# Is bot-service up?
curl http://localhost:8000/health
```

---

## Common Issues

**QR code not appearing:**
- `docker-compose logs whatsapp-service` — check for errors
- The QR only appears on first run or after session expiry

**Session expired (logged out):**
```bash
docker-compose down
rm -rf whatsapp-service/.baileys_auth
docker-compose up -d
docker-compose logs -f whatsapp-service  # scan the new QR
```

**Bot not responding to messages:**
1. Check bot-service logs for Gemini errors
2. Check that `GEMINI_API_KEY` is set correctly in `.env`
3. Verify both services are healthy (curl commands above)

**Cost logs not appearing:**
- `cost_logs/` is volume-mounted — check `bot-service/cost_logs/` on the host
- Logs only appear after the first Gemini API call completes successfully
