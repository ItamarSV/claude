export const toolDefinitions = [
  {
    name: 'search_web',
    description: 'Search the web for current information, news, facts, weather, prices, or anything that requires up-to-date data.',
    input_schema: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'The search query' },
      },
      required: ['query'],
    },
  },
  {
    name: 'fetch_url',
    description: 'Fetch the content of a specific URL and return the text.',
    input_schema: {
      type: 'object',
      properties: {
        url: { type: 'string', description: 'The URL to fetch' },
      },
      required: ['url'],
    },
  },
];

export async function search_web(query) {
  const url = `https://api.duckduckgo.com/?q=${encodeURIComponent(query)}&format=json&no_html=1&skip_disambig=1`;
  const res = await fetch(url, { headers: { 'User-Agent': 'WhatsApp-Assistant-Bot/1.0' } });
  const data = await res.json();

  const parts = [];
  if (data.AbstractText) parts.push(`Summary: ${data.AbstractText}`);
  if (data.Answer) parts.push(`Answer: ${data.Answer}`);
  if (data.Definition) parts.push(`Definition: ${data.Definition}`);
  if (data.RelatedTopics?.length) {
    const topics = data.RelatedTopics
      .filter((t) => t.Text)
      .slice(0, 5)
      .map((t) => `- ${t.Text}`);
    if (topics.length) parts.push(`Related:\n${topics.join('\n')}`);
  }

  return parts.length
    ? parts.join('\n\n')
    : `No instant answer found for "${query}". Try rephrasing.`;
}

export async function fetch_url(url) {
  const res = await fetch(url, {
    headers: { 'User-Agent': 'Mozilla/5.0 (compatible; WhatsApp-Assistant-Bot/1.0)' },
    signal: AbortSignal.timeout(10000),
  });
  const html = await res.text();
  // Strip tags, collapse whitespace
  const text = html
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/<style[\s\S]*?<\/style>/gi, '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 4000);
  return text || 'No readable content found at that URL.';
}

export async function runTool(name, input) {
  if (name === 'search_web') return search_web(input.query);
  if (name === 'fetch_url') return fetch_url(input.url);
  throw new Error(`Unknown tool: ${name}`);
}
