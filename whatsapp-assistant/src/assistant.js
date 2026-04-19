import Anthropic from '@anthropic-ai/sdk';
import { GoogleGenerativeAI } from '@google/generative-ai';
import { toolDefinitions, runTool } from './tools.js';

const anthropic = new Anthropic();

const SYSTEM_PROMPT = `You are a personal WhatsApp assistant. Help the user with anything they ask: answer questions, search for current info, summarize, draft messages, and more.
You have tools to search the web and fetch URLs — use them whenever the user asks about current events, news, weather, prices, or anything time-sensitive.
Be concise and practical. Reply in the same language the user writes in.`;

let provider = 'claude';
const conversationHistory = [];

async function callClaude(userMessage) {
  conversationHistory.push({ role: 'user', content: userMessage });

  let messages = [...conversationHistory];

  // Agentic loop: keep going until Claude stops calling tools
  while (true) {
    const response = await anthropic.messages.create({
      model: 'claude-sonnet-4-6',
      max_tokens: 1024,
      system: [{ type: 'text', text: SYSTEM_PROMPT, cache_control: { type: 'ephemeral' } }],
      tools: toolDefinitions,
      messages,
    });

    if (response.stop_reason === 'tool_use') {
      // Run each tool Claude requested
      const assistantMsg = { role: 'assistant', content: response.content };
      const toolResults = [];

      for (const block of response.content) {
        if (block.type !== 'tool_use') continue;
        console.log(`[Tool] ${block.name}(${JSON.stringify(block.input)})`);
        let result;
        try {
          result = await runTool(block.name, block.input);
        } catch (err) {
          result = `Error: ${err.message}`;
        }
        toolResults.push({ type: 'tool_result', tool_use_id: block.id, content: result });
      }

      messages = [...messages, assistantMsg, { role: 'user', content: toolResults }];
      continue;
    }

    // Final text response
    const reply = response.content.find((b) => b.type === 'text')?.text?.trim() ?? '';
    conversationHistory.push({ role: 'assistant', content: reply });
    if (conversationHistory.length > 40) conversationHistory.splice(0, 2);
    return reply;
  }
}

async function callGemini(userMessage, retries = 3) {
  const apiKey = process.env.GOOGLE_API_KEY;
  if (!apiKey) throw new Error('GOOGLE_API_KEY is not set in .env');

  const genAI = new GoogleGenerativeAI(apiKey);
  const model = genAI.getGenerativeModel({ model: 'gemini-2.0-flash', systemInstruction: SYSTEM_PROMPT });

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

export async function chat(userMessage) {
  if (provider === 'claude') {
    try {
      return { reply: await callClaude(userMessage), justSwitched: false };
    } catch (err) {
      if (isCreditError(err)) {
        console.warn('Claude credits exhausted — switching to Gemini.');
        provider = 'gemini';
        const reply = await callGemini(userMessage);
        return { reply, justSwitched: true };
      }
      throw err;
    }
  }
  return { reply: await callGemini(userMessage), justSwitched: false };
}
