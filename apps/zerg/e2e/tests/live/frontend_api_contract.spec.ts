/**
 * Frontend -> Backend API Contract Tests (Live)
 *
 * Validates that the deployed frontend's API calls actually reach the backend.
 * Catches path mismatches (double /api prefix), missing routes, and broken
 * frontend->backend integration that unit tests and mocked E2E miss.
 *
 * These run against the real deployed instance with a real browser.
 */

import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';

function trackApiErrors(page: Page): string[] {
  const apiErrors: string[] = [];
  page.on('response', (response) => {
    const url = response.url();
    if (url.includes('/api/') && response.status() >= 400) {
      apiErrors.push(`${response.status()} ${response.url()}`);
    }
  });
  return apiErrors;
}

async function waitForRouteReady(page: Page, path: string) {
  switch (path) {
    case '/settings':
      await expect(page.getByRole('heading', { name: 'LLM Providers' })).toBeVisible({ timeout: 10_000 });
      return;
    case '/dashboard':
      await expect(page.locator('[data-testid="create-fiche-btn"]')).toBeVisible({ timeout: 15_000 });
      return;
    case '/chat':
      await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 15_000 });
      return;
    default:
      throw new Error(`No readiness check defined for ${path}`);
  }
}

test.describe('Frontend API Contract', () => {
  for (const path of ['/settings', '/dashboard', '/chat'] as const) {
    test(`${path} loads without API errors`, async ({ context }) => {
      const page = await context.newPage();
      const apiErrors = trackApiErrors(page);

      await page.goto(path);
      await waitForRouteReady(page, path);

      expect(apiErrors, `API errors on ${path}: ${apiErrors.join(', ')}`).toHaveLength(0);
    });
  }

  test('No double /api prefix in any frontend request', async ({ context }) => {
    const page = await context.newPage();
    const doubleApiRequests: string[] = [];

    page.on('request', (request) => {
      const url = request.url();
      if (url.includes('/api/api/')) {
        doubleApiRequests.push(url);
      }
    });

    for (const path of ['/dashboard', '/settings', '/chat'] as const) {
      await page.goto(path);
      await waitForRouteReady(page, path);
    }

    expect(
      doubleApiRequests,
      'Double /api/api/ prefix detected\n' + doubleApiRequests.join('\n')
    ).toHaveLength(0);
  });
});
