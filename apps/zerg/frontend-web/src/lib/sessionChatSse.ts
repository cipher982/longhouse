export type SessionChatSseEvent = {
  eventType: string;
  data: string;
};

function parseEventBlock(block: string): SessionChatSseEvent | null {
  const lines = block.split('\n');
  let eventType = 'message';
  const dataLines: string[] = [];

  for (const line of lines) {
    if (!line) continue;
    if (line.startsWith('event: ')) {
      eventType = line.slice(7).trim() || 'message';
      continue;
    }
    if (line.startsWith('data: ')) {
      dataLines.push(line.slice(6));
    }
  }

  if (dataLines.length === 0) return null;
  return { eventType, data: dataLines.join('\n') };
}

export function consumeSessionChatSseBuffer(
  buffer: string,
  chunk: string,
  onEvent: (event: SessionChatSseEvent) => void,
): string {
  const normalized = `${buffer}${chunk}`.replace(/\r\n/g, '\n');
  const blocks = normalized.split('\n\n');
  const remainder = blocks.pop() ?? '';

  for (const block of blocks) {
    const parsed = parseEventBlock(block);
    if (parsed) onEvent(parsed);
  }

  return remainder;
}

export function flushSessionChatSseBuffer(
  buffer: string,
  onEvent: (event: SessionChatSseEvent) => void,
): void {
  const parsed = parseEventBlock(buffer.replace(/\r\n/g, '\n').trim());
  if (parsed) onEvent(parsed);
}
