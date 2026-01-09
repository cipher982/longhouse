/**
 * Smoke Test - Verify Core Infrastructure Works
 *
 * This test validates that all the infrastructure fixes are working:
 * 1. ES Modules are properly configured
 * 2. Backend can start in testing mode
 * 3. Frontend can be accessed
 * 4. Basic API calls work
 */

import { test, expect } from './fixtures';

test.describe('Infrastructure Smoke Test', () => {

  test('backend health check responds', async ({ request }) => {
    const response = await request.get('/health');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('status');
  });

  test('React frontend loads successfully', async ({ page }) => {
    await page.goto('/');

    // Wait for app to render - use a single unique element
    await expect(page.locator('[data-testid="create-agent-btn"]')).toBeVisible({ timeout: 15000 });

    // Check that the page loaded
    const title = await page.title();
    expect(title).toBeTruthy();
  });

  test('backend API returns data', async ({ request }) => {
    const response = await request.get('/api/agents');

    // Should return 200 or 401 (if auth required), not 500
    expect([200, 401]).toContain(response.status());
  });

  test('database is accessible in testing mode', async ({ request }) => {
    // Make a request that would require database access
    const response = await request.get('/api/threads');

    // Should not fail with database connection errors
    expect([200, 401, 404]).toContain(response.status());
  });

  test('visual testing dependencies available', async ({ page }) => {
    await page.goto('/');

    // Wait for app to be ready
    await expect(page.locator('[data-testid="create-agent-btn"]')).toBeVisible({ timeout: 15000 });

    // Test that we can take screenshots (basic visual testing requirement)
    const screenshot = await page.screenshot({ fullPage: true });
    expect(screenshot.length).toBeGreaterThan(1000); // Ensure it's a real screenshot
  });

});
