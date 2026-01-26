import { test, expect } from './fixtures';

test('Swarm map overlay renders and responds to live events', async ({ page }) => {
  await page.goto('/swarm');

  await expect(page.locator('.swarm-map-canvas canvas')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('.swarm-task-row').first()).toBeVisible({ timeout: 15000 });

  const modeToggle = page.getByRole('button', { name: 'Replay Mode' });
  await expect(modeToggle).toBeVisible();
  await modeToggle.click();

  await expect(page.locator('.swarm-task-empty')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('.swarm-panel-header .ui-badge', { hasText: 'Live' })).toBeVisible();

  await page.waitForFunction(() => Boolean((window as any).__jarvis?.eventBus));
  await page.evaluate(() => {
    (window as any).__jarvis.eventBus.emit('supervisor:started', {
      runId: 101,
      task: 'Ship logs',
      timestamp: Date.now(),
    });
  });

  await expect(page.locator('.swarm-task-row', { hasText: 'Ship logs' })).toBeVisible({ timeout: 15000 });
});
