# Operations Guide

## Current Setup (as deployed)
- **VM:** GCP `e2-micro`, zone `me-west1-c`, Ubuntu 24.04 minimal, 20GB disk
- **External IP:** 34.165.231.71
- **Bot WhatsApp number:** +972 55 992 5787 (eSIM)
- **Services:** systemd (no Docker) — `whatsapp-service` and `bot-service`
- **Repo:** `~/claude/whatsapp-bot/`

---

## First-Time Setup (for a fresh VM)

### 1. Get a bot phone number
- Use a real SIM/eSIM — virtual numbers (Twilio, Google Voice) are often blocked by WhatsApp
- Register the number as a WhatsApp account on your phone (WhatsApp → Add Account)

### 2. Provision VM
```bash
# Install Node.js 20 (required — Baileys needs 20+)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs git python3-pip python3-venv

# Clone repo
git clone https://github.com/ItamarSV/claude.git
cd claude/whatsapp-bot

# Create .env
cp .env.example .env
nano .env  # fill in GEMINI_API_KEY

# Install Node dependencies
cd whatsapp-service && npm install && cd ..

# Install Python dependencies
cd bot-service && python3 -m venv venv && venv/bin/pip install -r requirements.txt && cd ..
```

### 3. Create systemd services
```bash
# whatsapp-service
sudo tee /etc/systemd/system/whatsapp-service.service > /dev/null <<'EOF'
[Unit]
Description=WhatsApp Service (Baileys)
After=network.target
[Service]
WorkingDirectory=/home/USER/claude/whatsapp-bot/whatsapp-service
ExecStart=/usr/bin/node index.js
Restart=always
RestartSec=5
Environment=BOT_SERVICE_URL=http://localhost:8000
Environment=WHATSAPP_SERVICE_PORT=3000
User=USER
[Install]
WantedBy=multi-user.target
EOF

# bot-service
sudo tee /etc/systemd/system/bot-service.service > /dev/null <<'EOF'
[Unit]
Description=Bot Service (Gemini AI)
After=network.target whatsapp-service.service
[Service]
WorkingDirectory=/home/USER/claude/whatsapp-bot/bot-service
ExecStart=/home/USER/claude/whatsapp-bot/bot-service/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
EnvironmentFile=/home/USER/claude/whatsapp-bot/.env
Environment=WHATSAPP_SERVICE_URL=http://localhost:3000
User=USER
[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable whatsapp-service bot-service
sudo systemctl start whatsapp-service bot-service
```

### 4. Scan QR code (one-time)
- Open port 3000 in GCP firewall: `gcloud compute firewall-rules create allow-ws --allow tcp:3000 --target-tags http-server`
- Open `http://VM_EXTERNAL_IP:3000/qr` in browser
- Scan with the bot's WhatsApp account → Linked Devices → Link a Device
- Session saved to `whatsapp-service/.baileys_auth/`

### 5. Add bot to groups
- On your main WhatsApp → open any group → Add Participant → add the bot's number

---

## Environment Variables (`.env`)

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` | From Google AI Studio — required |
| `MAIN_GROUP_ID` | JID of the admin/control group (e.g. `120363428078252617@g.us`) — the Main group |

---

## Day-to-Day Commands

```bash
# View live logs
sudo journalctl -u whatsapp-service -u bot-service -f

# Restart after code change
cd ~/claude && git pull
cd whatsapp-bot/whatsapp-service && npm install  # if package.json changed
cd ~/claude/whatsapp-bot/bot-service && venv/bin/pip install -r requirements.txt  # if requirements changed
sudo systemctl restart whatsapp-service bot-service

# Check health
curl http://localhost:3000/health
curl http://localhost:8000/health
```

---

## Common Issues

**Bot not responding:**
1. `sudo journalctl -u whatsapp-service -u bot-service --since '5 minutes ago'`
2. Check for Gemini API errors (quota, model name, tool conflicts)
3. Check WhatsApp is still connected (`WhatsApp connected.` in logs)

**QR session expired (logged out):**
```bash
sudo systemctl stop whatsapp-service
rm -rf ~/claude/whatsapp-bot/whatsapp-service/.baileys_auth
sudo systemctl start whatsapp-service
# Open http://34.165.231.71:3000/qr and scan with WhatsApp Business app
```
Bot number is registered as **WhatsApp Business** (+972559925787). Must scan using the Business app, not regular WhatsApp.

**Bot not responding to @mentions (mention_only policy):**
- Check logs for `isBotMentioned=false`
- Likely cause: `botLid=null` — LID not loaded from `state.creds.me.lid`
- Fix: restart whatsapp-service (re-reads creds on startup)

**Gemini model issues:**
- `gemini-2.5-flash` = current working model
- Cannot combine `google_search` built-in tool with function declarations in same request
- To check available models: run `client.models.list()` with the API key

**Disk full:**
- Check with `df -h /`
- Remove snap packages: `sudo snap remove <package>`
- Clean apt cache: `sudo apt-get clean`
