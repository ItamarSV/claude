const {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  downloadMediaMessage,
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
let botNumber = null; // phone number, e.g. "972559925787"
let botLid = null;    // LID number, e.g. "36014072553559"

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
      botLid = sock.user?.lid?.split(':')[0] || sock.user?.lid?.split('@')[0] ||
               state.creds?.me?.lid?.split(':')[0] || state.creds?.me?.lid?.split('@')[0] || null;
      console.log(`WhatsApp connected. botNumber=${botNumber} botLid=${botLid}`);
      try { await axios.post(`${BOT_SERVICE_URL}/bot-online`); } catch (_) {}
    }
  });

  sock.ev.on('group-participants.update', async ({ id, participants, action }) => {
    console.log(`group-participants.update: action=${action} id=${id} participants=${JSON.stringify(participants)}`);
    const isBotInvolved = participants.some(p =>
      (botNumber && p.startsWith(botNumber + '@')) ||
      (botLid && p.startsWith(botLid + '@'))
    );
    if (!isBotInvolved) return;

    if (action === 'remove') {
      try {
        await axios.post(`${BOT_SERVICE_URL}/group-left`, { group_id: id });
        console.log(`Bot removed from group: ${id}`);
      } catch (err) {
        console.error('Failed to notify group-left:', err.message);
      }
    } else if (action === 'add') {
      try {
        const meta = await sock.groupMetadata(id);
        console.log(`Bot added to group: ${meta.subject}`);
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
      const id = group.id;
      if (!id) continue;
      try {
        const meta = await sock.groupMetadata(id);
        const isBot = (meta.participants || []).some(p =>
          (botNumber && p.id.startsWith(botNumber + '@')) ||
          (botLid && p.id.startsWith(botLid + '@'))
        );
        if (!isBot) continue;
        console.log(`groups.upsert: bot is member of ${meta.subject} (${id})`);
        await axios.post(`${BOT_SERVICE_URL}/group-joined`, {
          group_id: id,
          group_name: meta.subject || id,
        });
      } catch (err) {
        console.error('Failed to handle groups.upsert for', id, err.message);
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

      // Extract text from regular message, list response, or button response
      let text = null;
      const listResponse = msg.message.listResponseMessage?.singleSelectReply?.selectedRowId;
      const buttonResponse = msg.message.interactiveResponseMessage?.nativeFlowResponseMessage;
      if (listResponse) {
        text = listResponse;
      } else if (buttonResponse) {
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

      // If no text, check for audio message
      let audioData = null;
      let audioMime = null;
      if (!text && msg.message.audioMessage) {
        try {
          const buffer = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          audioData = buffer.toString('base64');
          audioMime = msg.message.audioMessage.mimetype || 'audio/ogg; codecs=opus';
          console.log(`Audio message from ${msg.pushName || 'unknown'}, size=${buffer.length}b`);
        } catch (err) {
          console.error('Failed to download audio message:', err.message);
        }
      }

      if (!text && !audioData) continue;

      // contextInfo can live under any message type
      const contextInfo =
        msg.message.extendedTextMessage?.contextInfo ||
        msg.message.audioMessage?.contextInfo ||
        msg.message.imageMessage?.contextInfo ||
        msg.message.videoMessage?.contextInfo ||
        {};

      const mentionedJids = contextInfo.mentionedJid || [];
      const isBotMentioned =
        (botNumber && mentionedJids.some(jid => jid.startsWith(botNumber + '@'))) ||
        (botLid && mentionedJids.some(jid => jid.startsWith(botLid + '@'))) ||
        false;
      if (mentionedJids.length > 0) {
        console.log(`mentions: ${JSON.stringify(mentionedJids)} isBotMentioned=${isBotMentioned} botNumber=${botNumber} botLid=${botLid}`);
      }

      const quotedParticipant = contextInfo.participant || '';
      const isReplyToBot =
        (botNumber && quotedParticipant.startsWith(botNumber + '@')) ||
        (botLid && quotedParticipant.startsWith(botLid + '@')) ||
        false;

      // Strip @mention markers from text so Gemini doesn't see raw IDs
      const cleanText = (text || '').replace(/@\d+/g, '').replace(/\s+/g, ' ').trim();

      const sender = msg.pushName || msg.key.participant?.split('@')[0] || 'Unknown';
      const timestamp = new Date(msg.messageTimestamp * 1000).toISOString();

      try {
        const payload = {
          group_id: jid,
          sender,
          sender_jid: msg.key.participant || '',
          text: cleanText || '',
          timestamp,
          is_bot_mentioned: isBotMentioned,
          is_reply_to_bot: isReplyToBot,
        };
        if (audioData) {
          payload.audio_data = audioData;
          payload.audio_mime = audioMime;
        }
        await axios.post(`${BOT_SERVICE_URL}/webhook`, payload);
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
    const { mention_jids } = req.body;
    let sentMsg;
    if (buttons && buttons.length > 0) {
      const lines = buttons.map((b, i) => `${i + 1}. ${b.text}`).join('\n');
      sentMsg = await sock.sendMessage(group_id, { text: `${text}\n\n${lines}\n\nReply *1* or *2*` });
    } else if (mention_jids && mention_jids.length > 0) {
      sentMsg = await sock.sendMessage(group_id, { text, mentions: mention_jids });
    } else {
      sentMsg = await sock.sendMessage(group_id, { text });
    }
    const message_key = sentMsg?.key ? { id: sentMsg.key.id, remote_jid: group_id, from_me: true, participant: '' } : null;
    res.json({ ok: true, message_key });
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

app.post('/react', async (req, res) => {
  const { group_id, message_key, emoji } = req.body;
  if (!group_id || !message_key || !emoji || !sock) return res.json({ ok: false });
  try {
    await sock.sendMessage(group_id, {
      react: {
        text: emoji,
        key: { remoteJid: message_key.remote_jid || group_id, fromMe: message_key.from_me ?? true, id: message_key.id, participant: message_key.participant || '' },
      },
    });
    res.json({ ok: true });
  } catch (err) {
    console.error('Failed to send reaction:', err.message);
    res.json({ ok: false });
  }
});

app.post('/typing', async (req, res) => {
  const { group_id } = req.body;
  if (!group_id || !sock) return res.json({ ok: false });
  try {
    await sock.sendPresenceUpdate('composing', group_id);
    res.json({ ok: true });
  } catch (err) {
    res.json({ ok: false });
  }
});

app.get('/group-participants', async (req, res) => {
  const { group_id } = req.query;
  if (!group_id) return res.status(400).json({ error: 'group_id required' });
  if (!sock) return res.status(503).json({ error: 'WhatsApp not connected' });
  try {
    const meta = await sock.groupMetadata(group_id);
    const participants = meta.participants.map(p => ({
      jid: p.id,
      name: p.notify || p.id.split('@')[0],
    }));
    res.json({ participants });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/group-name', async (req, res) => {
  const { group_id } = req.query;
  if (!group_id) return res.status(400).json({ error: 'group_id required' });
  if (!sock) return res.status(503).json({ error: 'WhatsApp not connected' });
  try {
    const meta = await sock.groupMetadata(group_id);
    res.json({ name: meta.subject || group_id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/health', (_req, res) => res.json({ ok: true }));

app.listen(PORT, () => console.log(`WhatsApp service listening on port ${PORT}`));

connectToWhatsApp();
