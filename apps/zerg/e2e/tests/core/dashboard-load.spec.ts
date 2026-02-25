/**
 * Dashboard Load Tests - Core Suite
 *
 * Tests that the dashboard renders correctly.
 *
 * CORE SUITE: 0 skipped, 0 flaky, retries: 0
 */

import { test, expect } from '../fixtures';

test.describe('Dashboard - Core', () => {
  test('dashboard renders with navigation', async ({ page }) => {
    await page.goto('/dashboard');

    // Header nav should be visible
    await expect(page.locator('.header-nav')).toBeVisible({ timeout: 15000 });

    // Timeline and Oikos tabs should be visible (streamlined nav)
    await expect(page.locator('.nav-tab:has-text("Timeline")')).toBeVisible();
    await expect(page.locator('.nav-tab:has-text("Oikos")')).toBeVisible();
  });

  test('create fiche button is present', async ({ page }) => {
    await page.goto('/dashboard');

    // Wait for the create fiche button to be visible and ready
    const createBtn = page.locator('[data-testid="create-fiche-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 15000 });
    await expect(createBtn).toBeEnabled();
  });
});
