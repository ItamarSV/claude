import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
} from '@whiskeysockets/baileys';
import QRCode from 'qrcode';
import { execSync } from 'child_process';
import path from 'path';
import pino from 'pino';
import { Boom } from '@hapi/boom';

const QR_PATH = path.resolve('qr.png');
const AUTH_PATH = './.baileys_auth';

const logger = pino({ level: 'silent' });

let sock = null;

async function connect(onMessage, onReady) {
  let waitingForQrScan = false;
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_PATH);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    logger,
    printQRInTerminal: false,
    browser: ['WhatsApp Assistant', 'Chrome', '1.0.0'],
    keepAliveIntervalMs: 10000,
    connectTimeoutMs: 60000,
    defaultQueryTimeoutMs: 60000,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      waitingForQrScan = true;
      await QRCode.toFile(QR_PATH, qr, { scale: 8 });
      const ascii = await QRCode.toString(qr, { type: 'terminal', small: true });
      console.log('\n' + ascii);
      console.log(`QR code saved → ${QR_PATH}`);
      try { execSync(`open "${QR_PATH}"`); } catch {}
    }

    if (connection === 'open') {
      waitingForQrScan = false;
      console.log('WhatsApp connected!');
      onReady();
    }

    if (connection === 'close') {
      const code = new Boom(lastDisconnect?.error)?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        console.error('Logged out. Delete .baileys_auth and restart to re-scan QR.');
        process.exit(1);
      }
      if (code === DisconnectReason.connectionReplaced) {
        console.error('Session taken over by another device. Exiting.');
        process.exit(1);
      }
      const delay = waitingForQrScan ? 60000 : 5000;
      console.warn(`Connection closed (${code}), reconnecting in ${delay / 1000}s…`);
      setTimeout(() => connect(onMessage, onReady), delay);
    }
  });

  // fromMe: true = messages YOU send from your phone (what we listen to in the group)
  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const msg of messages) {
      const text =
        msg.message?.conversation ||
        msg.message?.extendedTextMessage?.text ||
        msg.message?.imageMessage?.caption ||
        '';
      if (!text.trim()) continue;
      const jid = msg.key.remoteJid;
      const fromMe = msg.key.fromMe ?? false;
      onMessage({ jid, text, fromMe, msg });
    }
  });
}

export function createWhatsAppClient(onMessage) {
  return new Promise((resolve) => {
    connect(onMessage, () => resolve());
  });
}

export async function sendTyping(jid, typing = true, msgKey = null) {
  try {
    // Mark message as read first — required for typing indicator to show
    if (msgKey) await sock.readMessages([msgKey]);
    await sock.sendPresenceUpdate(typing ? 'composing' : 'paused', jid);
  } catch (err) {
    console.warn('sendTyping error:', err.message);
  }
}

export async function sendMessage(jid, text, allowedJid) {
  if (allowedJid && jid !== allowedJid) {
    throw new Error(`Blocked: bot may only send to ${allowedJid}`);
  }
  await sock.sendMessage(jid, { text });
}

export async function findGroupJid(groupName) {
  const groups = await sock.groupFetchAllParticipating();
  const match = Object.values(groups).find((g) => g.subject === groupName);
  return match?.id ?? null;
}
