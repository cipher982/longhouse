// Checks that the Owner column in the Dashboard only appears when the
// scope selector is switched to "All agents".

import { test, expect } from './fixtures';

test('Owner column toggles with scope selector', async ({ page }) => {
  await page.goto('/');

  // The checkbox is visually hidden - click the parent label.scope-toggle instead
  const scopeCheckbox = page.locator('[data-testid="dashboard-scope-toggle"]');
  const scopeLabel = page.locator('label.scope-toggle');
  await expect(scopeLabel).toBeVisible();

  // By default the dashboard shows the user's own agents -> no Owner column.
  await expect(scopeCheckbox).not.toBeChecked();
  await expect(page.locator('th', { hasText: 'Owner' })).toHaveCount(0);

  // Switch to All agents by clicking the visible label.
  await scopeLabel.click();
  await expect(scopeCheckbox).toBeChecked();
  await expect(page.locator('th', { hasText: 'Owner' })).toBeVisible();

  // Toggle back to "My agents" - Owner column should disappear.
  await scopeLabel.click();
  await expect(scopeCheckbox).not.toBeChecked();
  await expect(page.locator('th', { hasText: 'Owner' })).toHaveCount(0);
});
