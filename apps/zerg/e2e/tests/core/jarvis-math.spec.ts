/**
 * Core Jarvis Math E2E Test
 *
 * Validates deterministic scripted response for a simple math prompt.
 */

import { randomUUID } from 'node:crypto';

import { test, expect } from '../fixtures';
import { resetDatabase } from '../test-utils';

function parseSSEEvents(sseText: string): Array<{ event: string; data: unknown }> {
  const events: Array<{ event: string; data: unknown }> = [];
  const lines = sseText.replace(/\r\n/g, '\n').split('\n');
  let currentEvent = '';
  const currentDataLines: string[] = [];

  const pushEvent = () => {
    const currentData = currentDataLines.join('\n').trim();
    if (currentEvent && currentData) {
      try {
        events.push({ event: currentEvent, data: JSON.parse(currentData) });
      } catch {
        events.push({ event: currentEvent, data: currentData });
      }
      currentEvent = '';
      currentDataLines.length = 0;
    }
  };

  for (const line of lines) {
    if (line.startsWith('event:')) {
      currentEvent = line.substring(6).trim();
    } else if (line.startsWith('data:')) {
      currentDataLines.push(line.substring(5));
    } else if (line === '') {
      pushEvent();
    }
  }

  pushEvent();

  return events;
}

test.describe('Core Jarvis Math - Scripted LLM', () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('returns 4 for 2+2 prompt', async ({ request }) => {
    test.setTimeout(30000);

    const chatResponse = await request.post('/api/jarvis/chat', {
      data: {
        message: '2+2',
        message_id: randomUUID(),
        model: 'gpt-scripted',
      },
    });

    expect(chatResponse.status()).toBe(200);

    const sseText = await chatResponse.text();
    const events = parseSSEEvents(sseText);

    const completeEvent = events.find((event) => event.event === 'supervisor_complete');
    expect(completeEvent).toBeTruthy();

    const result = (completeEvent?.data as { payload?: { result?: string } })?.payload?.result ?? '';
    expect(result).toBe('4');
  });
});
