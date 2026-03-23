import { test, expect } from './fixtures';
import { resetDatabase } from './test-utils';
import { createAutomationViaUI } from './helpers/automation-helpers';

// Reset DB before each test to keep IDs predictable
// Uses strict reset that throws on failure to fail fast
test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

test('WebSocket envelopes include required fields for streaming events', async ({ page }) => {
  const wsMessages: any[] = [];

  page.on('websocket', ws => {
    ws.on('framereceived', event => {
      try {
        const message = JSON.parse(event.payload);
        wsMessages.push(message);
      } catch (error) {
        // Ignore non-JSON frames
      }
    });
  });

  const wsPromise = page.waitForEvent('websocket', { timeout: 10000 });

  const automationId = await createAutomationViaUI(page);
  await wsPromise;

  const chatBtn = page.locator(`[data-testid="chat-automation-${automationId}"]`);
  await expect(chatBtn).toBeVisible({ timeout: 10000 });
  await chatBtn.click();

  await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 10000 });
  await page.getByTestId('chat-input').fill('Say hello');
  await page.getByTestId('send-message-btn').click();

  await expect
    .poll(
      () => wsMessages.some((m: any) => ['stream_start', 'stream_chunk', 'stream_end'].includes(m.type)),
      { timeout: 15000 }
    )
    .toBe(true);

  const streamEnvelope = wsMessages.find((m: any) =>
    ['stream_start', 'stream_chunk', 'stream_end'].includes(m.type)
  );

  expect(streamEnvelope).toBeTruthy();
  expect(streamEnvelope).toEqual(expect.objectContaining({
    v: expect.any(Number),
    type: expect.any(String),
    topic: expect.any(String),
    ts: expect.any(Number),
    data: expect.anything(),
  }));

  expect(streamEnvelope.data?.thread_id).toBeDefined();
});
