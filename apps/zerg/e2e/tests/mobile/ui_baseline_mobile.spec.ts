import { test, expect, type Page } from '../fixtures';
import { waitForPageReady } from '../helpers/ready-signals';

const BASE_QUERY = 'clock=frozen&effects=off&seed=ui-baseline';

const MOBILE_PAGES = [
  { name: 'dashboard', path: `/dashboard?${BASE_QUERY}`, ready: 'page', navOpen: true },
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

async function captureBaseline(
  page: Page,
  path: string,
  name: string,
  ready: string,
  navOpen?: boolean
) {
  await page.goto(path);
  await waitForAppReady(page, ready);
  await expect(page).toHaveScreenshot(`${name}.png`, {
    fullPage: true,
    animations: 'disabled',
    maxDiffPixelRatio: 0.02, // Allow 2% pixel variance for font rendering differences
  });

  if (navOpen) {
    const toggle = page.locator('.mobile-menu-toggle');
    await toggle.waitFor({ state: 'visible', timeout: 5000 });
    await toggle.click();
    await expect(page.locator('.mobile-nav-drawer')).toHaveClass(/open/);
    await expect(page).toHaveScreenshot(`${name}-nav.png`, {
      fullPage: true,
      animations: 'disabled',
      maxDiffPixelRatio: 0.02, // Allow 2% pixel variance for font rendering differences
    });
  }
}

test.describe('UI baseline: mobile pages', () => {
  for (const pageDef of MOBILE_PAGES) {
    test(`baseline: ${pageDef.name}`, async ({ page }) => {
      await captureBaseline(page, pageDef.path, pageDef.name, pageDef.ready, pageDef.navOpen);
    });
  }
});
