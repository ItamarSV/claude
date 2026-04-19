import 'dotenv/config';
import { createWhatsAppClient, sendMessage, normalizeJid } from './whatsapp.js';
import { chat } from './assistant.js';

const OWNER_NUMBER = process.env.OWNER_NUMBER;

async function run() {
  if (!process.env.ANTHROPIC_API_KEY) {
    console.error('Error: ANTHROPIC_API_KEY is not set in your .env file.');
    process.exit(1);
  }
  if (!OWNER_NUMBER) {
    console.error('Error: OWNER_NUMBER is not set in your .env file. Set it to your WhatsApp number (e.g. 972501234567).');
    process.exit(1);
  }

  const ownerJid = normalizeJid(OWNER_NUMBER);

  const onMessage = async ({ from, text }) => {
    if (from !== ownerJid) return;

    console.log(`[${new Date().toLocaleTimeString()}] You: ${text}`);

    try {
      const { reply, justSwitched } = await chat(text);
      if (justSwitched) await sendMessage(ownerJid, "I've moved to Gemini");
      await sendMessage(ownerJid, reply);
      console.log(`[${new Date().toLocaleTimeString()}] Assistant: ${reply.slice(0, 80)}…`);
    } catch (err) {
      console.error('Error generating reply:', err.message);
    }
  };

  console.log('Initializing WhatsApp client…');
  await createWhatsAppClient(onMessage);

  console.log(`\n✅ Bot is live! Message the bot's number from ${OWNER_NUMBER} to start chatting.\n`);
  console.log('Press Ctrl+C to stop.\n');
}

run().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
