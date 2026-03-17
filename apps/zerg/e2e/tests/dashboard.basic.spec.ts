// Very first Playwright sanity-check.

import { test, expect } from './fixtures';

test('Dashboard route renders the automation surface', async ({ page }) => {
  // Load dashboard – webServer helper ensures the SPA is available.
  await page.goto('/dashboard');

  await expect(page.locator('#dashboard-container')).toBeVisible();
  await expect(page.getByTestId('create-fiche-btn')).toBeVisible();
  await expect(page.locator('#fiches-table-body')).toBeVisible();
});
