/**
 * Jarvis worker progress reconnection regression test.
 *
 * Reproduces the "orphan worker" scenario where tool events arrive before we know the jobId.
 * The UI must:
 * - show the WorkerProgress panel when tool events arrive
 * - reconcile to a real jobId on worker_complete
 * - clear the panel after supervisor_complete (no workers pending)
 */

import { test, expect } from '@playwright/test';

test('Jarvis worker progress reconciles orphan worker and clears on complete', async ({ page }) => {
  await page.goto('/chat', { waitUntil: 'domcontentloaded' });

  await expect(page.locator('.jarvis-container')).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('.text-input-container')).toBeVisible({ timeout: 15_000 });

  // Ensure the dev-only event bus injection is available.
  await page.waitForFunction(() => (window as any).__jarvis?.eventBus != null, null, { timeout: 15_000 });

  // Simulate tool activity arriving before we know the real jobId (common after refresh/reattach).
  await page.evaluate(() => {
    const bus = (window as any).__jarvis.eventBus;
    const workerId = 'e2e-worker-1';

    bus.emit('worker:tool_started', {
      workerId,
      toolName: 'ssh_exec',
      toolCallId: 'call-1',
      argsPreview: "{'host':'cube','command':'df -h'}",
      timestamp: Date.now(),
    });

    bus.emit('worker:tool_completed', {
      workerId,
      toolName: 'ssh_exec',
      toolCallId: 'call-1',
      durationMs: 42,
      resultPreview: '{"ok":true}',
      timestamp: Date.now(),
    });

    // Later, the canonical job lifecycle event arrives with a real jobId.
    bus.emit('supervisor:worker_complete', {
      jobId: 1,
      workerId,
      status: 'success',
      durationMs: 123,
      timestamp: Date.now(),
    });

    bus.emit('supervisor:complete', {
      runId: 1,
      result: 'done',
      status: 'success',
      timestamp: Date.now(),
    });
  });

  // Real-world bug: worker_complete was emitted repeatedly due to backend duplicate publishers.
  // Ensure the UI doesn't regress even if a duplicate slips through.
  await page.evaluate(() => {
    const bus = (window as any).__jarvis.eventBus;
    bus.emit('supervisor:worker_complete', {
      jobId: 1,
      workerId: 'e2e-worker-1',
      status: 'success',
      durationMs: 123,
      timestamp: Date.now(),
    });
  });

  const progress = page.locator('.worker-progress.worker-progress--active');
  await expect(progress).toBeVisible({ timeout: 5_000 });

  // Shows the orphan "pending details" worker while tool events stream.
  await expect(progress.locator('.worker-task')).toContainText('Worker (pending details)', { timeout: 5_000 });

  // After completion, the "running" count should disappear quickly.
  await expect(progress.locator('.supervisor-active-count')).toHaveCount(0, { timeout: 5_000 });

  // And the whole panel should clear shortly after supervisor_complete (2s debounce).
  await expect(progress).toHaveCount(0, { timeout: 10_000 });
});
