import { test, expect } from './fixtures';

test('Legacy /forum route redirects to timeline', async ({ page }) => {
  await page.goto('/forum');
  await expect(page).toHaveURL(/\/timeline(\/.*)?(\?.*)?$/, { timeout: 15000 });
  await expect(page.locator('.sessions-page, .sessions-hero-empty, .session-card').first()).toBeVisible({
    timeout: 15000,
  });
});
