/**
 * Frontend → Backend API Contract Tests (Live)
 *
 * Validates that the deployed frontend's API calls actually reach the backend.
 * Catches path mismatches (double /api prefix), missing routes, and broken
 * frontend→backend integration that unit tests and mocked E2E miss.
 *
 * These run against the real deployed instance with a real browser.
 */

import { test, expect } from './fixtures';

test.describe('Frontend API Contract', () => {
  test('Settings page loads LLM providers without API errors', async ({ context }) => {
    const page = await context.newPage();
    const apiErrors: string[] = [];

    // Capture failed API requests (4xx/5xx)
    page.on('response', (response) => {
      const url = response.url();
      if (url.includes('/api/') && response.status() >= 400) {
        apiErrors.push(`${response.status()} ${response.url()}`);
      }
    });

    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // LLM Providers section should render
    await expect(page.getByText('LLM Providers')).toBeVisible({ timeout: 10_000 });

    // No API errors should have occurred
    expect(apiErrors, `API errors on settings page: ${apiErrors.join(', ')}`).toHaveLength(0);
  });

  test('Dashboard page loads without API errors', async ({ context }) => {
    const page = await context.newPage();
    const apiErrors: string[] = [];

    page.on('response', (response) => {
      const url = response.url();
      if (url.includes('/api/') && response.status() >= 400) {
        apiErrors.push(`${response.status()} ${response.url()}`);
      }
    });

    await page.goto('/dashboard');
    await page.waitForLoadState('networkidle');

    // Dashboard should render
    await expect(page.locator('[data-testid="create-fiche-btn"]')).toBeVisible({ timeout: 15_000 });

    expect(apiErrors, `API errors on dashboard: ${apiErrors.join(', ')}`).toHaveLength(0);
  });

  test('Chat page loads without API errors', async ({ context }) => {
    const page = await context.newPage();
    const apiErrors: string[] = [];

    page.on('response', (response) => {
      const url = response.url();
      if (url.includes('/api/') && response.status() >= 400) {
        apiErrors.push(`${response.status()} ${response.url()}`);
      }
    });

    await page.goto('/chat');
    await page.waitForLoadState('networkidle');

    // Chat input should render
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 15_000 });

    expect(apiErrors, `API errors on chat page: ${apiErrors.join(', ')}`).toHaveLength(0);
  });

  test('No double /api prefix in any frontend request', async ({ context }) => {
    const page = await context.newPage();
    const doubleApiRequests: string[] = [];

    page.on('request', (request) => {
      const url = request.url();
      if (url.includes('/api/api/')) {
        doubleApiRequests.push(url);
      }
    });

    // Visit the pages most likely to trigger API calls
    for (const path of ['/dashboard', '/settings', '/chat']) {
      await page.goto(path);
      await page.waitForLoadState('networkidle');
    }

    expect(
      doubleApiRequests,
      `Double /api/api/ prefix detected:\n${doubleApiRequests.join('\n')}`
    ).toHaveLength(0);
  });
});
