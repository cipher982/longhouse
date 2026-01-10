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

    // Dashboard tab should be visible
    await expect(page.locator('.nav-tab:has-text("Dashboard")')).toBeVisible();
  });

  test('create agent button is present', async ({ page }) => {
    await page.goto('/');

    // Wait for the create agent button to be visible and ready
    const createBtn = page.locator('[data-testid="create-agent-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 15000 });
    await expect(createBtn).toBeEnabled();
  });
});
