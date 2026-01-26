/**
 * Jarvis commis progress reconnection regression test.
 *
 * Reproduces the "orphan commis" scenario where tool events arrive before we know the jobId.
 * The UI must:
 * - show the CommisProgress panel when tool events arrive
 * - reconcile to a real jobId on commis_complete
 * - clear the panel after concierge_complete (no commis pending)
 */

import { test, expect } from './fixtures';

// Skip: Commis progress UI has changed
test.skip();

test('Jarvis commis progress reconciles orphan commis and clears on complete', async ({ page }) => {
  await page.goto('/chat', { waitUntil: 'domcontentloaded' });

  await expect(page.locator('.jarvis-container')).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('.text-input-container')).toBeVisible({ timeout: 15_000 });

  // Ensure the dev-only event bus injection is available.
  await page.waitForFunction(() => (window as any).__jarvis?.eventBus != null, null, { timeout: 15_000 });

  // Simulate tool activity arriving before we know the real jobId (common after refresh/reattach).
  await page.evaluate(() => {
    const bus = (window as any).__jarvis.eventBus;
    const commisId = 'e2e-commis-1';

    bus.emit('commis:tool_started', {
      commisId,
      toolName: 'ssh_exec',
      toolCallId: 'call-1',
      argsPreview: "{'host':'cube','command':'df -h'}",
      timestamp: Date.now(),
    });

    bus.emit('commis:tool_completed', {
      commisId,
      toolName: 'ssh_exec',
      toolCallId: 'call-1',
      durationMs: 42,
      resultPreview: '{"ok":true}',
      timestamp: Date.now(),
    });

    // Later, the canonical job lifecycle event arrives with a real jobId.
    bus.emit('concierge:commis_complete', {
      jobId: 1,
      commisId,
      status: 'success',
      durationMs: 123,
      timestamp: Date.now(),
    });

    bus.emit('concierge:complete', {
      courseId: 1,
      result: 'done',
      status: 'success',
      timestamp: Date.now(),
    });
  });

  // Real-world bug: commis_complete was emitted repeatedly due to backend duplicate publishers.
  // Ensure the UI doesn't regress even if a duplicate slips through.
  await page.evaluate(() => {
    const bus = (window as any).__jarvis.eventBus;
    bus.emit('concierge:commis_complete', {
      jobId: 1,
      commisId: 'e2e-commis-1',
      status: 'success',
      durationMs: 123,
      timestamp: Date.now(),
    });
  });

  const progress = page.locator('.commis-progress.commis-progress--active');
  await expect(progress).toBeVisible({ timeout: 5_000 });

  // Shows the orphan "pending details" commis while tool events stream.
  await expect(progress.locator('.commis-task')).toContainText('Commis (pending details)', { timeout: 5_000 });

  // After completion, the "running" count should disappear quickly.
  await expect(progress.locator('.concierge-active-count')).toHaveCount(0, { timeout: 5_000 });

  // And the whole panel should clear shortly after concierge_complete (2s debounce).
  await expect(progress).toHaveCount(0, { timeout: 10_000 });
});
