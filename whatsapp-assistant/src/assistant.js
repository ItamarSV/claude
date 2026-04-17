import Anthropic from '@anthropic-ai/sdk';
import { GoogleGenerativeAI } from '@google/generative-ai';

const anthropic = new Anthropic();

const SYSTEM_PROMPT = `You are a personal WhatsApp assistant running on the user's own WhatsApp account.
You help the user manage their messages: suggest replies, summarize conversations, draft messages, and answer questions about their WhatsApp activity.
You have access to the user's recent chats when provided as context.
Be concise, friendly, and practical. Reply in the same language the user writes in.`;

let provider = 'claude';
const conversationHistory = [];

async function callClaude(userMessage) {
  conversationHistory.push({ role: 'user', content: userMessage });

  const response = await anthropic.messages.create({
    model: 'claude-sonnet-4-6',
    max_tokens: 1024,
    system: [{ type: 'text', text: SYSTEM_PROMPT, cache_control: { type: 'ephemeral' } }],
    messages: conversationHistory,
  });

  const reply = response.content[0].text.trim();
  conversationHistory.push({ role: 'assistant', content: reply });
  if (conversationHistory.length > 40) conversationHistory.splice(0, 2);
  return reply;
}

async function callGemini(userMessage, retries = 3) {
  const apiKey = process.env.GOOGLE_API_KEY;
  if (!apiKey) throw new Error('GOOGLE_API_KEY is not set in .env');

  const genAI = new GoogleGenerativeAI(apiKey);
  const model = genAI.getGenerativeModel({
    model: 'gemini-2.0-flash',
    systemInstruction: SYSTEM_PROMPT,
  });

  const history = conversationHistory.map((m) => ({
    role: m.role === 'assistant' ? 'model' : 'user',
    parts: [{ text: m.content }],
  }));

  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const geminiChat = model.startChat({ history });
      const result = await geminiChat.sendMessage(userMessage);
      const reply = result.response.text().trim();

      conversationHistory.push({ role: 'user', content: userMessage });
      conversationHistory.push({ role: 'assistant', content: reply });
      if (conversationHistory.length > 40) conversationHistory.splice(0, 2);
      return reply;
    } catch (err) {
      const is429 = err?.message?.includes('429') || err?.message?.includes('Too Many Requests');
      if (is429 && attempt < retries) {
        // Parse retry-after from error message if available, default to 60s
        const match = err.message.match(/retry in (\d+)/i);
        const waitSec = match ? parseInt(match[1]) + 2 : 60;
        console.warn(`Gemini rate limited — retrying in ${waitSec}s (attempt ${attempt + 1}/${retries})`);
        await new Promise((r) => setTimeout(r, waitSec * 1000));
        continue;
      }
      throw err;
    }
  }
}

function isCreditError(err) {
  return err?.status === 400 && err?.message?.includes('credit balance is too low');
}

export async function chat(userMessage, extraContext = '') {
  const fullMessage = extraContext ? `${extraContext}\n\nUser: ${userMessage}` : userMessage;

  if (provider === 'claude') {
    try {
      return { reply: await callClaude(fullMessage), justSwitched: false };
    } catch (err) {
      if (isCreditError(err)) {
        console.warn('Claude credits exhausted — switching to Gemini.');
        provider = 'gemini';
        const reply = await callGemini(fullMessage);
        return { reply, justSwitched: true };
      }
      throw err;
    }
  }

  return { reply: await callGemini(fullMessage), justSwitched: false };
}
