import { test, expect } from './fixtures';

test('Swarm map overlay renders and toggles live mode', async ({ page }) => {
  await page.goto('/swarm');

  await expect(page.locator('.swarm-map-canvas canvas')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('.swarm-task-row').first()).toBeVisible({ timeout: 15000 });

  const modeToggle = page.getByRole('button', { name: 'Replay Mode' });
  await expect(modeToggle).toBeVisible();
  await modeToggle.click();

  await expect(page.locator('.swarm-task-empty')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('.swarm-panel-header .ui-badge', { hasText: 'Live' })).toBeVisible();
});
