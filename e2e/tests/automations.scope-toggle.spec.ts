// Checks that the Owner column in the automations only appears when the
// scope selector is switched to "All automations".

import { test, expect } from './fixtures';
import { waitForAutomationsReady } from './helpers/test-helpers';

test('Owner column toggles with scope selector', async ({ page }) => {
  await waitForAutomationsReady(page);

  // The checkbox is visually hidden - click the parent label.scope-toggle instead
  const scopeCheckbox = page.locator('[data-testid="automations-scope-toggle"]');
  const scopeLabel = page.locator('label.scope-toggle');
  await expect(scopeLabel).toBeVisible();

  // By default the automations shows the user's own automations -> no Owner column.
  await expect(scopeCheckbox).not.toBeChecked();
  await expect(page.locator('th', { hasText: 'Owner' })).toHaveCount(0);

  // Switch to All automations by clicking the visible label.
  await scopeLabel.click();
  await expect(scopeCheckbox).toBeChecked();
  await expect(page.locator('th', { hasText: 'Owner' })).toBeVisible();

  // Toggle back to "My automations" - Owner column should disappear.
  await scopeLabel.click();
  await expect(scopeCheckbox).not.toBeChecked();
  await expect(page.locator('th', { hasText: 'Owner' })).toHaveCount(0);
});
