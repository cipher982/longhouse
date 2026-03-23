import { describe, expect, it } from 'vitest';

import { consumeSessionChatSseBuffer, flushSessionChatSseBuffer } from '../sessionChatSse';

describe('sessionChatSse', () => {
  it('parses split SSE blocks and preserves remainder', () => {
    const events: Array<{ eventType: string; data: string }> = [];
    let buffer = '';

    buffer = consumeSessionChatSseBuffer(
      buffer,
      'event: system\ndata: {"type":"session_started"}\n\nevent: assistant_delta\ndata: {"text":"hel',
      (event) => events.push(event),
    );

    expect(events).toEqual([{ eventType: 'system', data: '{"type":"session_started"}' }]);
    expect(buffer).toContain('event: assistant_delta');

    buffer = consumeSessionChatSseBuffer(
      buffer,
      'lo","accumulated":"hello"}\n\n',
      (event) => events.push(event),
    );

    expect(events).toEqual([
      { eventType: 'system', data: '{"type":"session_started"}' },
      { eventType: 'assistant_delta', data: '{"text":"hello","accumulated":"hello"}' },
    ]);
    expect(buffer).toBe('');
  });

  it('flushes the final buffered event without a trailing blank line', () => {
    const events: Array<{ eventType: string; data: string }> = [];
    let buffer = '';

    buffer = consumeSessionChatSseBuffer(
      buffer,
      'event: done\ndata: {"session_id":"child-1","shipped_session_id":"child-1"}',
      (event) => events.push(event),
    );

    expect(events).toEqual([]);
    flushSessionChatSseBuffer(buffer, (event) => events.push(event));

    expect(events).toEqual([
      { eventType: 'done', data: '{"session_id":"child-1","shipped_session_id":"child-1"}' },
    ]);
  });
});
