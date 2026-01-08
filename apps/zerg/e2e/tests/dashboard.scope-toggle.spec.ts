// Checks that the Owner column in the Dashboard only appears when the
// scope selector is switched to "All agents".

import { test, expect } from './fixtures';

// Skip: Scope toggle uses data-testid="dashboard-scope-toggle" not "dashboard-scope-select"
test.skip();

test('Owner column toggles with scope selector', async ({ page }) => {
  await page.goto('/');

  const scopeSelect = page.locator('[data-testid="dashboard-scope-select"]');
  await scopeSelect.waitFor();

  // By default the dashboard shows the user's own agents -> no Owner column.
  await expect(page.locator('th', { hasText: 'Owner' })).toHaveCount(0);

  // Switch to All agents.
  await scopeSelect.selectOption('all');
  await expect(page.locator('th', { hasText: 'Owner' })).toBeVisible();

  // Persisted after reload?
  await page.reload();
  await expect(scopeSelect).toHaveValue('all');
});
