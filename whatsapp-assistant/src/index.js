import 'dotenv/config';
import { createWhatsAppClient, findAssistantChat, fetchRecentChats } from './whatsapp.js';
import { chat } from './assistant.js';

const ASSISTANT_CHAT_NAME = process.env.ASSISTANT_CHAT_NAME ?? 'Assistant';

async function run() {
  if (!process.env.ANTHROPIC_API_KEY) {
    console.error('Error: ANTHROPIC_API_KEY is not set in your .env file.');
    process.exit(1);
  }

  const client = createWhatsAppClient();

  await new Promise((resolve, reject) => {
    client.on('ready', resolve);
    client.on('auth_failure', reject);
    client.initialize();
    console.log('Initializing WhatsApp client…');
  });

  await new Promise((r) => setTimeout(r, 4000));

  const assistantChat = await findAssistantChat(client, ASSISTANT_CHAT_NAME);

  if (!assistantChat) {
    console.log(`\n⚠️  No WhatsApp group named "${ASSISTANT_CHAT_NAME}" found.`);
    console.log(`Please create a WhatsApp group called "${ASSISTANT_CHAT_NAME}" and restart.`);
    await client.destroy();
    return;
  }

  console.log(`\n✅ Found assistant chat: "${ASSISTANT_CHAT_NAME}"`);

  const assistantChatId = assistantChat.id._serialized;

  // Counter incremented before each bot send; message_create decrements it to skip bot echoes.
  // This works because message_create fires synchronously during sendMessage.
  let pendingBotReplies = 0;

  const sendReply = async (text) => {
    pendingBotReplies++;
    await assistantChat.sendMessage(text);
  };

  // Register listener BEFORE sending welcome so no events are missed
  client.on('message_create', async (msg) => {
    if (msg.id.remote !== assistantChatId) return;
    if (!msg.body) return;

    // Absorb the bot's own outgoing messages
    if (msg.fromMe && pendingBotReplies > 0) {
      pendingBotReplies--;
      return;
    }

    // Only respond to messages sent by the user from their phone
    if (!msg.fromMe) return;

    console.log(`[${new Date().toLocaleTimeString()}] User: ${msg.body}`);

    let context = '';
    if (/messages?|chats?|recent|unread|inbox/i.test(msg.body)) {
      const recentChats = await fetchRecentChats(client, 15);
      if (recentChats.length) {
        context = `Recent unread messages:\n` + recentChats
          .map((c) => `- ${c.chatName}${c.isGroup ? ' (group)' : ''}: "${c.lastMessage}"`)
          .join('\n');
      }
    }

    try {
      const { reply, justSwitched } = await chat(msg.body, context);
      if (justSwitched) await sendReply("I've moved to Gemini");
      await sendReply(reply);
      console.log(`[${new Date().toLocaleTimeString()}] Assistant: ${reply.slice(0, 80)}…`);
    } catch (err) {
      console.error('Error generating reply:', err.message);
    }
  });

  await sendReply('👋 Assistant ready! Ask me anything about your WhatsApp messages.');
  console.log('Listening for messages. Press Ctrl+C to stop.\n');
}

run().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
