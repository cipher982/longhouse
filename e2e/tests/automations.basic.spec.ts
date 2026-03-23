// Very first Playwright sanity-check.

import { test, expect } from './fixtures';

test('Automations route renders the automation surface', async ({ page }) => {
  // Load automations – webServer helper ensures the SPA is available.
  await page.goto('/automations');

  await expect(page.locator('#automations-container')).toBeVisible();
  await expect(page.getByTestId('create-automation-btn')).toBeVisible();
  await expect(page.locator('#automations-table-body')).toBeVisible();
});
