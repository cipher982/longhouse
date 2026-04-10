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
 * - http://localhost:30080/chat → Oikos chat UI
 * - http://localhost:30080/automations → Zerg automations
 * - Cross-navigation between /chat and /automations
 *
 * Skipped when UNIFIED_BASE_URL is not set or the unified proxy is unavailable.
 */

import { test, expect } from './fixtures';

// Use env var or default to unified proxy port
const UNIFIED_URL = process.env.UNIFIED_BASE_URL || 'http://localhost:30080';

// Check if unified proxy is available before running tests
test.beforeAll(async ({ request }) => {
  try {
    const response = await request.get(`${UNIFIED_URL}/api/health`, { timeout: 5000 });
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
    await expect(page).toHaveTitle(/Longhouse/, { timeout: 10000 });
  });

  test('chat page loads at /chat', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/chat`);

    // Wait for Oikos chat UI to load (text input container)
    await expect(page.locator('.text-input-container')).toBeVisible({ timeout: 10000 });
    // Verify we're in the Oikos container
    await expect(page.locator('.oikos-container')).toBeVisible();
  });

  test('automations page loads at /automations', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/automations`, { waitUntil: 'domcontentloaded' });

    await expect(page.locator('.header-nav')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="create-automation-btn"]')).toBeVisible();
  });

  test('Chat tab is visible from automations', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/automations`, { waitUntil: 'domcontentloaded' });

    await expect(page.locator('.header-nav')).toBeVisible({ timeout: 15000 });

    const oikosTab = page.locator('.nav-tab:has-text("Chat")');
    await expect(oikosTab).toBeVisible();
  });

  test('Chat tab navigates from automations to chat', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/automations`, { waitUntil: 'domcontentloaded' });

    await expect(page.locator('.header-nav')).toBeVisible({ timeout: 15000 });

    await page.click('.nav-tab:has-text("Chat")');

    await expect(page.locator('.text-input-container')).toBeVisible({ timeout: 15000 });
    await expect(page).toHaveURL(/\/chat/);
  });

  test('automations route stays directly reachable after visiting chat', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/chat`);

    await expect(page.locator('.text-input-container')).toBeVisible({ timeout: 15000 });
    await page.goto(`${UNIFIED_URL}/automations`, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('[data-testid="create-automation-btn"]')).toBeVisible({ timeout: 15000 });
    await expect(page).toHaveURL(/\/automations/);
  });

  test('API health check via unified proxy', async ({ page }) => {
    const response = await page.request.get(`${UNIFIED_URL}/api/health`);
    expect(response.status()).toBe(200);
  });

});
