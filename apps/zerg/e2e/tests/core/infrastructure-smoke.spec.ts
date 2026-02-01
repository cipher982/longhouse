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
    const response = await request.get('/api/system/health');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('status');
    expect(body).toHaveProperty('db');
  });

  test('frontend loads successfully', async ({ page }) => {
    await page.goto('/');

    // Root should redirect to timeline in auth-disabled E2E mode.
    await expect(page).toHaveURL(/\/timeline/);

    // Wait for layout to mount.
    await expect(page.locator('[data-testid="app-container"]')).toBeVisible({ timeout: 15000 });

    // Verify page has a title
    const title = await page.title();
    expect(title).toBeTruthy();
  });

  test('backend API returns valid response', async ({ request }) => {
    const response = await request.get('/api/system/info');

    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('auth_disabled');
  });

  test('database is accessible', async ({ request }) => {
    // Lightweight DB check via system health probe
    const response = await request.get('/api/system/health');

    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body?.db?.status).toBe('ok');
  });
});
