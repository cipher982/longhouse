/**
 * Infrastructure Smoke Tests - Core Suite
 *
 * These tests verify that the core infrastructure is working:
 * - Backend health endpoint
 * - Frontend loads
 * - Database is accessible
 *
 * CORE SUITE: 0 skipped, 0 flaky, retries: 0
 */

import { test, expect } from '../fixtures';

test.describe('Infrastructure - Core', () => {
  test('backend health check responds', async ({ request }) => {
    const response = await request.get('/health');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('status');
  });

  test('frontend loads successfully', async ({ page }) => {
    await page.goto('/');

    // Wait for the app to render with create button visible
    await expect(page.locator('[data-testid="create-fiche-btn"]')).toBeVisible({ timeout: 15000 });

    // Verify page has a title
    const title = await page.title();
    expect(title).toBeTruthy();
  });

  test('backend API returns valid response', async ({ request }) => {
    const response = await request.get('/api/fiches');

    // Should return 200 (or 401 if auth required), not 500
    expect([200, 401]).toContain(response.status());
  });

  test('database is accessible', async ({ request }) => {
    // Make a request that requires database access
    const response = await request.get('/api/threads');

    // Should not fail with database connection errors
    expect([200, 401, 404]).toContain(response.status());
  });
});
