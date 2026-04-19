import pkg from 'whatsapp-web.js';
import QRCode from 'qrcode';
import { execSync } from 'child_process';
import path from 'path';

const { Client, LocalAuth } = pkg;
const QR_PATH = path.resolve('qr.png');

export function createWhatsAppClient() {
  const client = new Client({
    authStrategy: new LocalAuth({ dataPath: './.wwebjs_auth' }),
    puppeteer: {
      headless: true,
      executablePath: process.env.CHROMIUM_PATH || undefined,
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
      ],
      protocolTimeout: 120000,
    },
  });

  client.on('qr', async (qr) => {
    await QRCode.toFile(QR_PATH, qr, { scale: 8 });
    console.log(`QR code saved → ${QR_PATH}`);
    try { execSync(`open "${QR_PATH}"`); } catch {}
  });

  client.on('authenticated', () => console.log('WhatsApp authenticated.'));
  client.on('auth_failure', (msg) => { console.error('Auth failed:', msg); process.exit(1); });
  client.on('disconnected', (reason) => console.warn('Disconnected:', reason));

  return client;
}

export async function findAssistantChat(client, chatName) {
  const chats = await client.getChats();
  return chats.find((c) => c.name === chatName) ?? null;
}

export async function fetchChatContext(chat, limit = 10) {
  try {
    const messages = await chat.fetchMessages({ limit });
    return messages.map((m) => ({ fromMe: m.fromMe, body: m.body }));
  } catch {
    return [];
  }
}

export async function fetchRecentChats(client, limit = 15) {
  const chats = await client.getChats();
  const results = [];
  for (const chat of chats.slice(0, limit)) {
    let messages;
    try { messages = await chat.fetchMessages({ limit: 3 }); } catch { continue; }
    if (!messages.length) continue;
    const last = messages[messages.length - 1];
    if (!last.body || last.fromMe) continue;
    results.push({ chatName: chat.name || chat.id.user, isGroup: chat.isGroup, lastMessage: last.body });
  }
  return results;
}
