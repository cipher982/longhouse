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
    const response = await request.get('/api/health');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('status');
    expect(body).toHaveProperty('checks');
    expect(body.checks).toHaveProperty('database');
  });

  test('frontend loads successfully', async ({ page }) => {
    // Navigate directly to /timeline (root shows landing page in dev mode)
    await page.goto('/timeline');

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
    const response = await request.get('/api/health');

    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body?.checks?.database?.status).toBe('pass');
  });
});
