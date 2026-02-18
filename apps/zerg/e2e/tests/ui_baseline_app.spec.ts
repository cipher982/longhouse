import { test, expect, type Page } from './fixtures';
import { waitForPageReady } from './helpers/ready-signals';
import { APP_PAGES, type PageDef } from './helpers/page-list';
import { resetDatabase } from './test-utils';

test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

async function waitForAppReady(page: Page, mode: PageDef['ready']) {
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

async function captureBaseline(page: Page, path: string, name: string, ready: PageDef['ready']) {
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
        !e.includes('ResizeObserver') && // React ResizeObserver warnings
        !e.includes('x-test-commis') && // test header trips CORS on external fonts
        !e.includes('fonts.gstatic.com') &&
        !e.includes('fontshare.com') &&
        !e.includes('Failed to load resource') &&
        !e.includes('useOikosApp') &&
        !e.includes('Failed to fetch bootstrap') &&
        !e.includes('Failed to check for active run')
      );

      const critical404s = notFoundUrls.filter(url =>
        !url.includes('favicon') &&
        !url.includes('/api/') && // API 404s might be expected (no data)
        !url.includes('fonts.gstatic.com') &&
        !url.includes('fontshare.com')
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
