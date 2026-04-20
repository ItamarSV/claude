const {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
} = require('@whiskeysockets/baileys');
const express = require('express');
const axios = require('axios');
const qrcode = require('qrcode-terminal');
const pino = require('pino');
const path = require('path');

const BOT_SERVICE_URL = process.env.BOT_SERVICE_URL || 'http://localhost:8000';
const PORT = parseInt(process.env.WHATSAPP_SERVICE_PORT || '3000');
const AUTH_FOLDER = path.join(__dirname, '.baileys_auth');

const logger = pino({ level: 'silent' });

let sock = null;

async function connectToWhatsApp() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_FOLDER);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    logger,
    printQRInTerminal: false,
    generateHighQualityLinkPreview: false,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.log('\n--- Scan this QR code with WhatsApp ---');
      qrcode.generate(qr, { small: true });
      console.log('---------------------------------------\n');
    }

    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = code !== DisconnectReason.loggedOut;
      console.log(`Connection closed (code ${code}). Reconnecting: ${shouldReconnect}`);
      if (shouldReconnect) {
        setTimeout(connectToWhatsApp, 5000);
      } else {
        console.log('Logged out. Delete .baileys_auth and restart to re-scan QR.');
      }
    }

    if (connection === 'open') {
      console.log('WhatsApp connected.');
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;

    for (const msg of messages) {
      if (!msg.message) continue;

      const jid = msg.key.remoteJid;
      if (!jid || !jid.endsWith('@g.us')) continue;

      if (msg.key.fromMe) continue;

      const text =
        msg.message.conversation ||
        msg.message.extendedTextMessage?.text ||
        null;

      if (!text) continue;

      const sender = msg.pushName || msg.key.participant?.split('@')[0] || 'Unknown';
      const timestamp = new Date(msg.messageTimestamp * 1000).toISOString();

      try {
        await axios.post(`${BOT_SERVICE_URL}/webhook`, {
          group_id: jid,
          sender,
          text,
          timestamp,
        });
      } catch (err) {
        console.error('Failed to forward message to bot-service:', err.message);
      }
    }
  });
}

const app = express();
app.use(express.json());

app.post('/send', async (req, res) => {
  const { group_id, text } = req.body;
  if (!group_id || !text) {
    return res.status(400).json({ error: 'group_id and text are required' });
  }
  if (!sock) {
    return res.status(503).json({ error: 'WhatsApp not connected' });
  }
  try {
    await sock.sendMessage(group_id, { text });
    res.json({ ok: true });
  } catch (err) {
    console.error('Failed to send message:', err.message);
    res.status(500).json({ error: err.message });
  }
});

app.get('/health', (_req, res) => res.json({ ok: true }));

app.listen(PORT, () => console.log(`WhatsApp service listening on port ${PORT}`));

connectToWhatsApp();
