/**
 * Sessions Timeline E2E Tests
 *
 * Tests the agent sessions list and detail pages.
 * Note: These tests require sessions to be present in the database.
 * In a fresh test DB, the empty state will be shown.
 */

import { test, expect } from './fixtures';

test.describe('Timeline Page', () => {
  test('Timeline tab renders and shows list or empty state', async ({ page }) => {
    // Navigate to timeline page
    await page.goto('/timeline');

    // Wait for page to be ready
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // The header nav should be visible with Timeline tab
    await expect(page.locator('.header-nav')).toBeVisible();
    await expect(page.locator('.nav-tab:has-text("Timeline")')).toBeVisible();

    // Should show either sessions list or empty state
    const hasSessions = await page.locator('.session-card').count() > 0;
    const hasEmptyState = await page.locator('.ui-empty-state, .timeline-empty').first().isVisible();

    expect(hasSessions || hasEmptyState).toBe(true);
  });

  test('Filter bar is visible and interactive', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Filter bar should be visible
    const filterBar = page.locator('.sessions-filter-bar');
    await expect(filterBar).toBeVisible();

    // Filter selects should be present
    await expect(filterBar.locator('select').first()).toBeVisible();

    // Search input should be present
    await expect(filterBar.locator('input[type="search"]')).toBeVisible();
  });

  test('Filter by provider updates URL', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Select a provider filter
    const providerSelect = page.locator('.sessions-filter-select').nth(1);
    await providerSelect.selectOption('claude');

    // URL should update with provider param
    await expect(page).toHaveURL(/provider=claude/);
  });

  test('Search input triggers debounced query', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Type in search
    const searchInput = page.locator('input[type="search"]');
    await searchInput.fill('test query');

    // Wait for debounce and URL update
    await page.waitForTimeout(400);

    // URL should include query param
    await expect(page).toHaveURL(/query=test\+query|query=test%20query/);
  });

  test('Clear filters button removes all filters', async ({ page }) => {
    // Navigate with pre-set filters
    await page.goto('/timeline?provider=claude&project=zerg');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Clear button should be visible
    const clearButton = page.locator('button:has-text("Clear")');
    await expect(clearButton).toBeVisible();

    // Click clear
    await clearButton.click();

    // URL should no longer have filter params
    await expect(page).toHaveURL('/timeline');
  });
});

test.describe('Session Detail Page', () => {
  test('Shows error for invalid session ID', async ({ page }) => {
    // Navigate to a non-existent session
    await page.goto('/timeline/00000000-0000-0000-0000-000000000000');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Should show error state
    await expect(page.locator('.ui-empty-state')).toBeVisible();
    await expect(page.locator('text=Error loading session')).toBeVisible();

    // Back button should be visible
    const backButton = page.locator('button:has-text("Back")');
    await expect(backButton).toBeVisible();
  });

  test('Back button navigates to sessions list', async ({ page }) => {
    // Navigate to invalid session to get error state
    await page.goto('/timeline/00000000-0000-0000-0000-000000000000');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Click back button
    await page.locator('button:has-text("Back")').click();

    // Should be back on timeline list
    await expect(page).toHaveURL('/timeline');
  });
});

test.describe('Timeline Navigation', () => {
  test('Timeline tab in nav links to /timeline', async ({ page }) => {
    await page.goto('/dashboard');
    await page.waitForSelector('.header-nav', { timeout: 10000 });

    // Click Timeline tab
    await page.locator('.nav-tab:has-text("Timeline")').click();

    // Should navigate to timeline page
    await expect(page).toHaveURL('/timeline');
  });
});
