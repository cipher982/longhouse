import { test, expect } from './fixtures';

test('The Forum renders and shows session desks', async ({ page }) => {
  await page.goto('/forum');

  await expect(page.locator('.forum-map-canvas canvas')).toBeVisible({ timeout: 15000 });

  const loadDemo = page.getByRole('button', { name: /Load demo data/i });
  if (await loadDemo.isVisible()) {
    await loadDemo.click();
  }

  await expect(page.locator('.forum-session-row').first()).toBeVisible({ timeout: 15000 });
});
