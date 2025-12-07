/**
 * Unified Frontend Smoke Tests
 *
 * Tests the unified frontend routing at http://localhost:30080
 * - Landing page loads at /
 * - Jarvis chat loads at /chat
 * - Dashboard loads at /dashboard
 * - Cross-navigation links work between apps
 */

import { test, expect } from '@playwright/test';

const UNIFIED_URL = 'http://localhost:30080';

test.describe('Unified Frontend Navigation', () => {

  test('landing page loads at /', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/`);
    await page.waitForLoadState('networkidle');

    // Zerg landing page should have the brand
    await expect(page.locator('text=Swarmlet')).toBeVisible({ timeout: 10000 });
  });

  test('chat page loads at /chat', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/chat`);
    await page.waitForLoadState('networkidle');

    // Jarvis chat should have the PTT button
    await expect(page.locator('#pttBtn')).toBeVisible({ timeout: 10000 });
    // Should have the Jarvis title
    await expect(page.locator('#appTitle')).toContainText('Jarvis');
  });

  test('dashboard page loads at /dashboard', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/dashboard`);
    await page.waitForLoadState('networkidle');

    // Dashboard should show the Agent Dashboard tab as active
    await expect(page.locator('[data-testid="global-dashboard-tab"]')).toBeVisible({ timeout: 10000 });
  });

  test('chat tab visible in Zerg dashboard', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/dashboard`);
    await page.waitForLoadState('networkidle');

    // Chat tab should be visible in the nav
    const chatTab = page.locator('[data-testid="global-chat-tab"]');
    await expect(chatTab).toBeVisible({ timeout: 10000 });
    await expect(chatTab).toHaveAttribute('href', '/chat');
  });

  test('dashboard link visible in Jarvis chat', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/chat`);
    await page.waitForLoadState('networkidle');

    // Dashboard link should be visible in header
    const dashboardLink = page.locator('a[href="/dashboard"]');
    await expect(dashboardLink).toBeVisible({ timeout: 10000 });
    await expect(dashboardLink).toContainText('Dashboard');
  });

  test('chat tab navigates from dashboard to chat', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/dashboard`);
    await page.waitForLoadState('networkidle');

    // Click the chat tab
    await page.click('[data-testid="global-chat-tab"]');
    await page.waitForLoadState('networkidle');

    // Should now be on chat page
    await expect(page).toHaveURL(/\/chat/);
    await expect(page.locator('#pttBtn')).toBeVisible({ timeout: 10000 });
  });

  test('dashboard link navigates from chat to dashboard', async ({ page }) => {
    await page.goto(`${UNIFIED_URL}/chat`);
    await page.waitForLoadState('networkidle');

    // Click the dashboard link
    await page.click('a[href="/dashboard"]');
    await page.waitForLoadState('networkidle');

    // Should now be on dashboard
    await expect(page).toHaveURL(/\/dashboard/);
    await expect(page.locator('[data-testid="global-dashboard-tab"]')).toBeVisible({ timeout: 10000 });
  });

  test('API health check via unified proxy', async ({ page }) => {
    const response = await page.request.get(`${UNIFIED_URL}/api/health`);
    expect(response.status()).toBe(200);
  });

});
