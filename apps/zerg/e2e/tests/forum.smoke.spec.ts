import { test, expect } from './fixtures';

test('The Forum renders and responds to live events', async ({ page }) => {
  await page.goto('/forum');

  await expect(page.locator('.forum-map-canvas canvas')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('.forum-task-row').first()).toBeVisible({ timeout: 15000 });

  const modeToggle = page.getByRole('button', { name: 'Replay Mode' });
  await expect(modeToggle).toBeVisible();
  await modeToggle.click();

  await expect(page.locator('.forum-task-empty')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('.forum-panel-header .ui-badge', { hasText: 'Live' })).toBeVisible();

  await page.waitForFunction(() => Boolean((window as any).__jarvis?.eventBus));
  await page.waitForFunction(
    () => (window as any).__jarvis?.eventBus?.listenerCount?.('supervisor:started') > 0,
  );

  await page.evaluate(() => {
    (window as any).__jarvis.eventBus.emit('supervisor:started', {
      runId: 101,
      task: 'Ship logs',
      timestamp: Date.now(),
    });
  });

  await expect(page.locator('.forum-task-row', { hasText: 'Ship logs' })).toBeVisible({ timeout: 15000 });
});
