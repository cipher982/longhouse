/**
 * Automations Load Tests - Core Suite
 *
 * Tests that the automations renders correctly.
 *
 * CORE SUITE: 0 skipped, 0 flaky, retries: 0
 */

import { test, expect } from '../fixtures';

test.describe('Automations - Core', () => {
  test('automations renders with navigation', async ({ page }) => {
    await page.goto('/automations');

    // Header nav should be visible
    await expect(page.locator('.header-nav')).toBeVisible({ timeout: 15000 });

    // Timeline and Chat tabs should be visible (streamlined nav)
    await expect(page.locator('.nav-tab:has-text("Timeline")')).toBeVisible();
    await expect(page.locator('.nav-tab:has-text("Chat")')).toBeVisible();
  });

  test('create automation button is present', async ({ page }) => {
    await page.goto('/automations');

    // Wait for the create automation button to be visible and ready
    const createBtn = page.locator('[data-testid="create-automation-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 15000 });
    await expect(createBtn).toBeEnabled();
  });
});
