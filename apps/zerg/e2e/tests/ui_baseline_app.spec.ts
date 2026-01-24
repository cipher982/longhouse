import { test, expect, type Page } from './fixtures';
import { waitForPageReady } from './helpers/ready-signals';

const BASE_QUERY = 'clock=frozen&effects=off&seed=ui-baseline';

const APP_PAGES = [
  { name: 'dashboard', path: `/dashboard?${BASE_QUERY}`, ready: 'page' },
  { name: 'chat', path: `/chat?${BASE_QUERY}`, ready: 'page' },
  { name: 'canvas', path: `/canvas?${BASE_QUERY}`, ready: 'page' },
  { name: 'settings', path: `/settings?${BASE_QUERY}`, ready: 'settings' },
  { name: 'profile', path: `/profile?${BASE_QUERY}`, ready: 'page' },
  { name: 'runners', path: `/runners?${BASE_QUERY}`, ready: 'page' },
  { name: 'integrations', path: `/settings/integrations?${BASE_QUERY}`, ready: 'page' },
  { name: 'knowledge', path: `/settings/knowledge?${BASE_QUERY}`, ready: 'page' },
  { name: 'contacts', path: `/settings/contacts?${BASE_QUERY}`, ready: 'page' },
  { name: 'admin', path: `/admin?${BASE_QUERY}`, ready: 'page' },
  { name: 'traces', path: `/traces?${BASE_QUERY}`, ready: 'page' },
  { name: 'reliability', path: `/reliability?${BASE_QUERY}`, ready: 'page' },
];

async function waitForAppReady(page: Page, mode: string) {
  if (mode === 'page') {
    await waitForPageReady(page, { timeout: 20000 });
    return;
  }

  if (mode === 'settings') {
    await waitForPageReady(page, { timeout: 20000 });
    await expect(page.locator('.settings-page-container')).toBeVisible();
    await expect(page.locator('form.profile-form')).toBeVisible();
  }
}

async function captureBaseline(page: Page, path: string, name: string, ready: string) {
  await page.goto(path);
  await waitForAppReady(page, ready);
  await expect(page).toHaveScreenshot(`${name}.png`, {
    fullPage: true,
    animations: 'disabled',
  });
}

test.describe('UI baseline: app pages', () => {
  for (const pageDef of APP_PAGES) {
    test(`baseline: ${pageDef.name}`, async ({ page }) => {
      await captureBaseline(page, pageDef.path, pageDef.name, pageDef.ready);
    });
  }
});

/**
 * Smoke test: ensure no 404s or console errors on page load.
 * This catches issues like missing assets, broken imports, etc.
 */
test.describe('Console error check', () => {
  test('no 404 errors or console errors on app pages', async ({ page }) => {
    const errors: string[] = [];
    const notFoundUrls: string[] = [];

    // Capture console errors
    page.on('console', msg => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
      }
    });

    // Capture 404 responses
    page.on('response', response => {
      if (response.status() === 404) {
        notFoundUrls.push(response.url());
      }
    });

    // Visit each page and check for errors
    for (const pageDef of APP_PAGES) {
      errors.length = 0;
      notFoundUrls.length = 0;

      await page.goto(pageDef.path);
      await waitForAppReady(page, pageDef.ready);

      // Filter out known acceptable errors (e.g., expected 404s for missing data)
      const criticalErrors = errors.filter(e =>
        !e.includes('favicon') && // favicon 404 is common
        !e.includes('ResizeObserver') // React ResizeObserver warnings
      );

      const critical404s = notFoundUrls.filter(url =>
        !url.includes('favicon') &&
        !url.includes('/api/') // API 404s might be expected (no data)
      );

      if (criticalErrors.length > 0) {
        throw new Error(`Console errors on ${pageDef.name}:\n${criticalErrors.join('\n')}`);
      }

      if (critical404s.length > 0) {
        throw new Error(`404 errors on ${pageDef.name}:\n${critical404s.join('\n')}`);
      }
    }
  });
});
