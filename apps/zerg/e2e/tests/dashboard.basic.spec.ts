// Very first Playwright sanity-check.

import { test, expect } from './fixtures';

test('Dashboard tab renders', async ({ page }) => {
  // Load dashboard – webServer helper ensures the SPA is available.
  await page.goto('/dashboard');

  // The header nav should be visible with Dashboard tab
  await expect(page.locator('.header-nav')).toBeVisible();
  await expect(page.locator('.nav-tab:has-text("Dashboard")')).toBeVisible();
});
