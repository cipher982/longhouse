import { test, expect, type Page } from './fixtures';
import { waitForPageReady } from './helpers/ready-signals';
import { resetDatabase } from './test-utils';

const BASE_QUERY = 'clock=frozen&effects=on&seed=glass-smoke';

const APP_PAGES = [
  { name: 'chat', path: `/chat?${BASE_QUERY}`, ready: 'page' },
  { name: 'dashboard', path: `/dashboard?${BASE_QUERY}`, ready: 'page' },
  { name: 'canvas', path: `/canvas?${BASE_QUERY}`, ready: 'page' },
  { name: 'settings', path: `/settings?${BASE_QUERY}`, ready: 'settings' },
];

test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

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

test.describe('Visual smoke: glass baseline', () => {
  for (const pageDef of APP_PAGES) {
    test(`snapshot: ${pageDef.name}`, async ({ page }) => {
      await page.goto(pageDef.path);
      await waitForAppReady(page, pageDef.ready);
      await expect(page).toHaveScreenshot(`${pageDef.name}.png`, {
        fullPage: true,
        animations: 'disabled',
      });
    });
  }
});

/**
 * Smoke test: ensure no 404s or console errors on key pages.
 */
test.describe('Visual smoke: console error check', () => {
  test('no 404 errors or console errors on key pages', async ({ page }) => {
    const errors: string[] = [];
    const notFoundUrls: string[] = [];

    page.on('console', msg => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
      }
    });

    page.on('response', response => {
      if (response.status() === 404) {
        notFoundUrls.push(response.url());
      }
    });

    for (const pageDef of APP_PAGES) {
      errors.length = 0;
      notFoundUrls.length = 0;

      await page.goto(pageDef.path);
      await waitForAppReady(page, pageDef.ready);

      const criticalErrors = errors.filter(e =>
        !e.includes('favicon') &&
        !e.includes('ResizeObserver') &&
        !e.includes('x-test-commis') &&
        !e.includes('fonts.gstatic.com') &&
        !e.includes('fontshare.com') &&
        !e.includes('Failed to load resource') &&
        !e.includes('useOikosApp') &&
        !e.includes('Failed to fetch bootstrap') &&
        !e.includes('Failed to check for active run')
      );

      const critical404s = notFoundUrls.filter(url =>
        !url.includes('favicon') &&
        !url.includes('/api/') &&
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
