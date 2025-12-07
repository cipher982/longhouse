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

    // Wait for PTT button - indicates Jarvis loaded
    await expect(page.locator('#pttBtn')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('#appTitle')).toContainText('Jarvis');
  });

  test('dashboard page loads at /dashboard', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/dashboard`, { waitUntil: 'domcontentloaded' });

    // Wait for dashboard tab - indicates Zerg loaded
    // Note: If auth redirect happens, this may fail - ensure AUTH_DISABLED=1 in .env
    await expect(page.locator('[data-testid="global-dashboard-tab"]')).toBeVisible({ timeout: 15000 });
  });

  test('chat tab visible in Zerg dashboard', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/dashboard`, { waitUntil: 'domcontentloaded' });

    // Wait for dashboard to load
    await expect(page.locator('[data-testid="global-dashboard-tab"]')).toBeVisible({ timeout: 15000 });

    // Chat tab should be visible in the nav
    const chatTab = page.locator('[data-testid="global-chat-tab"]');
    await expect(chatTab).toBeVisible();
    await expect(chatTab).toHaveAttribute('href', '/chat');
  });

  test('dashboard link visible in Jarvis chat', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/chat`);

    // Wait for chat to load
    await expect(page.locator('#pttBtn')).toBeVisible({ timeout: 10000 });

    // Dashboard link should be visible in header
    const dashboardLink = page.locator('a[href="/dashboard"]');
    await expect(dashboardLink).toBeVisible();
    await expect(dashboardLink).toContainText('Dashboard');
  });

  test('chat tab navigates from dashboard to chat', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/dashboard`, { waitUntil: 'domcontentloaded' });

    // Wait for dashboard to load
    await expect(page.locator('[data-testid="global-dashboard-tab"]')).toBeVisible({ timeout: 15000 });

    // Click the chat tab
    await page.click('[data-testid="global-chat-tab"]');

    // Wait for chat page to load (PTT button visible)
    await expect(page.locator('#pttBtn')).toBeVisible({ timeout: 15000 });
    await expect(page).toHaveURL(/\/chat/);
  });

  test('dashboard link navigates from chat to dashboard', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/chat`);

    // Wait for chat to load
    await expect(page.locator('#pttBtn')).toBeVisible({ timeout: 15000 });

    // Click the dashboard link
    await page.click('a[href="/dashboard"]');

    // Wait for dashboard to load
    await expect(page.locator('[data-testid="global-dashboard-tab"]')).toBeVisible({ timeout: 15000 });
    await expect(page).toHaveURL(/\/dashboard/);
  });

  test('API health check via unified proxy', async ({ page }) => {
    const response = await page.request.get(`${UNIFIED_URL}/health`);
    expect(response.status()).toBe(200);
  });

});
