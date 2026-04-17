export function formatSuggestion({ chatName, isGroup, lastMessage, suggestedReply }) {
  const type = isGroup ? '(Group)' : '(Direct)';
  return [
    `━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━`,
    `Chat:             ${chatName} ${type}`,
    `Last Message:     ${lastMessage}`,
    `Suggested Reply:  ${suggestedReply}`,
  ].join('\n');
}

export function printHeader(count) {
  console.log(`\n${'═'.repeat(34)}`);
  console.log(`  WhatsApp Assistant — ${count} chats`);
  console.log(`${'═'.repeat(34)}\n`);
}

export function printFooter() {
  console.log(`\n${'━'.repeat(34)}`);
  console.log('Done. Review suggestions above.');
}
