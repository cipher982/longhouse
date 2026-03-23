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
    await page.goto('/');

    // Root serves the landing page (no redirect).
    await expect(page).toHaveURL(/\/$/);

    // Wait for landing page to mount.
    await expect(page.locator('.landing-page')).toBeVisible({ timeout: 15000 });

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
