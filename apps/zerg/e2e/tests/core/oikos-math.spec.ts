/**
 * Core Oikos Math E2E Test
 *
 * Validates deterministic scripted response for a simple math prompt.
 */

import { randomUUID } from 'node:crypto';

import { test, expect } from '../fixtures';
import { postSseAndCollect } from '../helpers/sse';
import { resetDatabase } from '../test-utils';

test.describe('Core Oikos Math - Scripted LLM', () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('returns 4 for 2+2 prompt', async ({ request, backendUrl, commisId }) => {
    test.setTimeout(30000);

    const events = await postSseAndCollect({
      backendUrl,
      commisId,
      path: '/api/oikos/chat',
      payload: {
        message: '2+2',
        message_id: randomUUID(),
        model: 'gpt-scripted',
      },
      stopEvent: 'oikos_complete',
      timeoutMs: 30000,
    });

    const completeEvent = events.find((event) => event.event === 'oikos_complete');
    expect(completeEvent).toBeTruthy();

    const result = (completeEvent?.data as { payload?: { result?: string } })?.payload?.result ?? '';
    expect(result).toBe('4');
  });
});
