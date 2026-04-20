const {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
} = require('@whiskeysockets/baileys');
const express = require('express');
const axios = require('axios');
const QRCode = require('qrcode');
const pino = require('pino');
const path = require('path');

let latestQR = null;

const BOT_SERVICE_URL = process.env.BOT_SERVICE_URL || 'http://localhost:8000';
const PORT = parseInt(process.env.WHATSAPP_SERVICE_PORT || '3000');
const AUTH_FOLDER = path.join(__dirname, '.baileys_auth');

const logger = pino({ level: 'silent' });

let sock = null;
let botNumber = null; // e.g. "972559925787" — set after connection opens

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
      latestQR = qr;
      console.log('New QR code ready. Open http://YOUR_VM_IP:3000/qr to scan.');
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
      botNumber = sock.user?.id?.split(':')[0] || sock.user?.id?.split('@')[0] || null;
      console.log(`WhatsApp connected. Bot number: ${botNumber}`);
    }
  });

  sock.ev.on('group-participants.update', async ({ id, participants, action }) => {
    console.log(`group-participants.update: action=${action} id=${id} participants=${participants} botNumber=${botNumber}`);
    if (action === 'add' && botNumber && participants.some(p => p.startsWith(botNumber + '@'))) {
      try {
        const meta = await sock.groupMetadata(id);
        await axios.post(`${BOT_SERVICE_URL}/group-joined`, {
          group_id: id,
          group_name: meta.subject || id,
        });
      } catch (err) {
        console.error('Failed to notify group-joined:', err.message);
      }
    }
  });

  sock.ev.on('groups.upsert', async (groups) => {
    for (const group of groups) {
      console.log(`groups.upsert: id=${group.id} subject=${group.subject}`);
      try {
        await axios.post(`${BOT_SERVICE_URL}/group-joined`, {
          group_id: group.id,
          group_name: group.subject || group.id,
        });
      } catch (err) {
        console.error('Failed to notify group-joined (upsert):', err.message);
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;

    for (const msg of messages) {
      if (!msg.message) continue;

      const jid = msg.key.remoteJid;
      if (!jid || !jid.endsWith('@g.us')) continue;

      if (msg.key.fromMe) continue;

      // Extract text from regular message or button response
      let text = null;
      const buttonResponse = msg.message.interactiveResponseMessage?.nativeFlowResponseMessage;
      if (buttonResponse) {
        try {
          const params = JSON.parse(buttonResponse.paramsJson || '{}');
          text = params.id || null;
        } catch (_) {}
      } else {
        text =
          msg.message.conversation ||
          msg.message.extendedTextMessage?.text ||
          null;
      }

      if (!text) continue;

      const mentionedJids = msg.message.extendedTextMessage?.contextInfo?.mentionedJid || [];
      const isBotMentioned = botNumber
        ? mentionedJids.some(jid => jid.startsWith(botNumber + '@'))
        : false;

      const sender = msg.pushName || msg.key.participant?.split('@')[0] || 'Unknown';
      const timestamp = new Date(msg.messageTimestamp * 1000).toISOString();

      try {
        await axios.post(`${BOT_SERVICE_URL}/webhook`, {
          group_id: jid,
          sender,
          text,
          timestamp,
          is_bot_mentioned: isBotMentioned,
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
  const { group_id, text, buttons } = req.body;
  if (!group_id || !text) {
    return res.status(400).json({ error: 'group_id and text are required' });
  }
  if (!sock) {
    return res.status(503).json({ error: 'WhatsApp not connected' });
  }
  try {
    if (buttons && buttons.length > 0) {
      const lines = buttons.map((b, i) => `${i + 1}. ${b.text}`).join('\n');
      await sock.sendMessage(group_id, { text: `${text}\n\n${lines}\n\nReply *1* or *2*` });
    } else {
      await sock.sendMessage(group_id, { text });
    }
    res.json({ ok: true });
  } catch (err) {
    console.error('Failed to send message:', err.message);
    res.status(500).json({ error: err.message });
  }
});

app.get('/qr', async (_req, res) => {
  if (!latestQR) {
    return res.send('<h2>No QR code yet — WhatsApp may already be connected or still loading. Refresh in a few seconds.</h2>');
  }
  try {
    const dataUrl = await QRCode.toDataURL(latestQR, { width: 400 });
    res.send(`
      <html><body style="display:flex;flex-direction:column;align-items:center;font-family:sans-serif;padding:40px">
        <h2>Scan with WhatsApp (bot account)</h2>
        <img src="${dataUrl}" />
        <p>This page auto-refreshes every 20 seconds. QR codes expire — refresh if scan fails.</p>
        <script>setTimeout(() => location.reload(), 20000)</script>
      </body></html>
    `);
  } catch (e) {
    res.status(500).send('Failed to generate QR image.');
  }
});

app.get('/health', (_req, res) => res.json({ ok: true }));

app.listen(PORT, () => console.log(`WhatsApp service listening on port ${PORT}`));

connectToWhatsApp();
