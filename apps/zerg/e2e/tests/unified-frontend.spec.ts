/**
 * Unified Frontend Smoke Tests
 *
 * Tests the unified frontend routing via nginx proxy (typically port 30080).
 * These tests require the unified stack to be running (make dev).
 *
 * NOTE: These tests are currently skipped by default because Playwright's webServer
 * configuration starts test-specific backend/frontend on ports 8001/8002, not the
 * unified Docker stack on 30080. To run these tests:
 *
 * 1. Start unified stack: `make dev`
 * 2. Run tests with custom config: `UNIFIED_BASE_URL=http://localhost:30080 bunx playwright test unified-frontend.spec.ts --config=playwright-unified.config.js`
 *
 * OR perform manual smoke tests (recommended for now):
 * - http://localhost:30080/ → Landing page
 * - http://localhost:30080/chat → Jarvis chat UI
 * - http://localhost:30080/dashboard → Zerg dashboard
 * - Cross-navigation between /chat and /dashboard
 *
 * Skipped when UNIFIED_BASE_URL is not set or the unified proxy is unavailable.
 */

import { test, expect } from '@playwright/test';

// Use env var or default to unified proxy port
const UNIFIED_URL = process.env.UNIFIED_BASE_URL || 'http://localhost:30080';

// Check if unified proxy is available before running tests
test.beforeAll(async ({ request }) => {
  try {
    const response = await request.get(`${UNIFIED_URL}/health`, { timeout: 5000 });
    if (!response.ok()) {
      test.skip(true, `Unified proxy not available at ${UNIFIED_URL}`);
    }
  } catch {
    test.skip(true, `Unified proxy not reachable at ${UNIFIED_URL} - run 'make dev' to start unified stack`);
  }
});

test.describe('Unified Frontend Navigation', () => {

  test('landing page loads at /', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/`);

    // Wait for page title - use toHaveTitle() which correctly reads document.title
    await expect(page).toHaveTitle(/Swarmlet/, { timeout: 10000 });
    await expect(page).toHaveURL(/\/(dashboard)?$/);
  });

  test('chat page loads at /chat', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/chat`);

    // Wait for Jarvis chat UI to load (text input container)
    await expect(page.locator('.text-input-container')).toBeVisible({ timeout: 10000 });
    // Verify we're in the Jarvis container
    await expect(page.locator('.jarvis-container')).toBeVisible();
  });

  test('dashboard page loads at /dashboard', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/dashboard`, { waitUntil: 'domcontentloaded' });

    // Wait for header nav to load - indicates Zerg loaded
    // Note: If auth redirect happens, this may fail - ensure AUTH_DISABLED=1 in .env
    await expect(page.locator('.header-nav')).toBeVisible({ timeout: 15000 });
    // Dashboard tab should be active
    await expect(page.locator('.nav-tab--active:has-text("Dashboard")')).toBeVisible();
  });

  test('chat tab visible in Zerg dashboard', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/dashboard`, { waitUntil: 'domcontentloaded' });

    // Wait for dashboard to load
    await expect(page.locator('.header-nav')).toBeVisible({ timeout: 15000 });

    // Chat tab should be visible in the nav
    const chatTab = page.locator('.nav-tab:has-text("Chat")');
    await expect(chatTab).toBeVisible();
  });

  test('dashboard link visible in Jarvis chat', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/chat`);

    // Wait for chat to load
    await expect(page.locator('.text-input-container')).toBeVisible({ timeout: 10000 });

    // Dashboard tab should be visible in header nav
    const dashboardTab = page.locator('.nav-tab:has-text("Dashboard")');
    await expect(dashboardTab).toBeVisible();
  });

  test('chat tab navigates from dashboard to chat', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/dashboard`, { waitUntil: 'domcontentloaded' });

    // Wait for dashboard to load
    await expect(page.locator('.header-nav')).toBeVisible({ timeout: 15000 });

    // Click the chat tab
    await page.click('.nav-tab:has-text("Chat")');

    // Wait for chat page to load
    await expect(page.locator('.text-input-container')).toBeVisible({ timeout: 15000 });
    await expect(page).toHaveURL(/\/chat/);
  });

  test('dashboard link navigates from chat to dashboard', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/chat`);

    // Wait for chat to load
    await expect(page.locator('.text-input-container')).toBeVisible({ timeout: 15000 });

    // Click the dashboard tab
    await page.click('.nav-tab:has-text("Dashboard")');

    // Wait for dashboard to load
    await expect(page.locator('.nav-tab--active:has-text("Dashboard")')).toBeVisible({ timeout: 15000 });
    await expect(page).toHaveURL(/\/dashboard/);
  });

  test('API health check via unified proxy', async ({ page }) => {
    const response = await page.request.get(`${UNIFIED_URL}/health`);
    expect(response.status()).toBe(200);
  });

});
