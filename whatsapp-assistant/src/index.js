import 'dotenv/config';
import { createWhatsAppClient, sendMessage, findGroupJid } from './whatsapp.js';
import { chat } from './assistant.js';

const ASSISTANT_CHAT_NAME = process.env.ASSISTANT_CHAT_NAME ?? 'Assistant';

async function run() {
  if (!process.env.ANTHROPIC_API_KEY) {
    console.error('Error: ANTHROPIC_API_KEY is not set in your .env file.');
    process.exit(1);
  }

  let assistantJid = null;

  const onMessage = async ({ jid, text, fromMe }) => {
    // Only handle messages from the Assistant group
    // fromMe: false = sent from your phone (different device); fromMe: true = bot's own sendMessage
    if (!assistantJid || jid !== assistantJid) return;
    if (fromMe) return;

    console.log(`[${new Date().toLocaleTimeString()}] You: ${text}`);

    try {
      const { reply, justSwitched } = await chat(text);
      if (justSwitched) await sendMessage(assistantJid, "I've moved to Gemini", assistantJid);
      await sendMessage(assistantJid, reply, assistantJid);
      console.log(`[${new Date().toLocaleTimeString()}] Assistant: ${reply.slice(0, 80)}…`);
    } catch (err) {
      console.error('Error generating reply:', err.message);
    }
  };

  console.log('Initializing WhatsApp client…');
  await createWhatsAppClient(onMessage);

  // Give the store a moment to populate chat list
  await new Promise((r) => setTimeout(r, 3000));

  assistantJid = await findGroupJid(ASSISTANT_CHAT_NAME);

  if (!assistantJid) {
    console.log(`\n⚠️  No WhatsApp group named "${ASSISTANT_CHAT_NAME}" found.`);
    console.log(`Create a WhatsApp group called "${ASSISTANT_CHAT_NAME}" and restart.`);
    process.exit(1);
  }

  console.log(`\n✅ Found "${ASSISTANT_CHAT_NAME}" group (${assistantJid})`);
  await sendMessage(assistantJid, '👋 Assistant ready! Send me a message.', assistantJid);
  console.log('Listening for messages. Press Ctrl+C to stop.\n');
}

run().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
