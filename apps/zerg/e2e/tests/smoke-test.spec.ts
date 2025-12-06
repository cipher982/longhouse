/**
 * Smoke Test - Verify Core Infrastructure Works
 *
 * This test validates that all the infrastructure fixes are working:
 * 1. ES Modules are properly configured
 * 2. Backend can start in testing mode with SQLite
 * 3. Frontend can be accessed
 * 4. Basic API calls work
 */

import { test, expect } from '@playwright/test';

test.describe('Infrastructure Smoke Test', () => {

  test('backend health check responds', async ({ page }) => {
    console.log('🔍 Testing backend health endpoint...');

    const response = await page.request.get('http://localhost:47300/health');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('status');

    console.log('✅ Backend health check passed');
  });

  test('React frontend loads successfully', async ({ page }) => {
    console.log('🔍 Testing React frontend...');

    await page.goto('http://localhost:47200/');
    await page.waitForLoadState('networkidle');

    // Check that the page loaded
    const title = await page.title();
    expect(title).toBeTruthy();

    console.log('✅ React frontend loaded successfully');
  });

  test('backend API returns data', async ({ page }) => {
    console.log('🔍 Testing backend API functionality...');

    const response = await page.request.get('http://localhost:47300/api/agents');

    // Should return 200 or 401 (if auth required), not 500
    expect([200, 401]).toContain(response.status());

    console.log('✅ Backend API responded correctly');
  });

  test('database is accessible in testing mode', async ({ page }) => {
    console.log('🔍 Testing database access in testing mode...');

    // Make a request that would require database access
    const response = await page.request.get('http://localhost:47300/api/threads');

    // Should not fail with database connection errors
    expect([200, 401, 404]).toContain(response.status());

    console.log('✅ Database accessible in testing mode');
  });

  test('visual testing dependencies available', async ({ page }) => {
    console.log('🔍 Testing visual testing capabilities...');

    // Test that we can take screenshots (basic visual testing requirement)
    await page.goto('http://localhost:47200/');
    await page.waitForLoadState('networkidle');

    const screenshot = await page.screenshot({ fullPage: true });
    expect(screenshot.length).toBeGreaterThan(1000); // Ensure it's a real screenshot

    console.log('✅ Visual testing capabilities working');
  });

});
