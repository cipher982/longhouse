/**
 * Jarvis non-blocking worker UX regression test.
 *
 * Ensures chat input re-enables after supervisor_complete even if
 * a background worker is still running (simulated by a hanging /chat fetch).
 */

import { test, expect } from './fixtures';
import { waitForReadyFlag, waitForEventBusAvailable } from './helpers/ready-signals';

test('chat input unblocks after supervisor_complete while worker runs', async ({ page }) => {
  await page.goto('/chat', { waitUntil: 'domcontentloaded' });
  await waitForReadyFlag(page, 'chatReady');
  await waitForEventBusAvailable(page);

  const input = page.getByTestId('chat-input');
  const sendBtn = page.getByTestId('send-message-btn');

  await expect(input).toBeVisible({ timeout: 10_000 });
  await expect(sendBtn).toBeVisible({ timeout: 10_000 });

  // Patch fetch so /api/jarvis/chat never resolves (simulates long-running SSE stream).
  await page.evaluate(() => {
    const originalFetch = window.fetch.bind(window);
    (window as any).__e2e_original_fetch = originalFetch;
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
      if (url.includes('/api/jarvis/chat')) {
        return new Promise<Response>(() => {});
      }
      return originalFetch(input, init);
    };
  });

  await input.fill('Start a workspace analysis');
  await sendBtn.click();
  await expect(sendBtn).toBeDisabled({ timeout: 5_000 });

  // Pre-fill follow-up while the first run is still "in-flight".
  await input.fill('Follow up while worker runs');

  // Simulate background worker spawned + supervisor completes immediately.
  await page.evaluate(() => {
    const bus = (window as any).__jarvis?.eventBus;
    if (!bus) return;
    bus.emit('supervisor:worker_spawned', {
      jobId: 101,
      task: 'workspace',
      timestamp: Date.now(),
    });
    bus.emit('supervisor:complete', {
      runId: 1,
      result: 'Worker started',
      status: 'success',
      timestamp: Date.now(),
    });
  });

  // Input should re-enable even though worker is still "running".
  await expect(sendBtn).toBeEnabled({ timeout: 5_000 });

  await sendBtn.click();
  await expect(sendBtn).toBeDisabled({ timeout: 5_000 });
});
