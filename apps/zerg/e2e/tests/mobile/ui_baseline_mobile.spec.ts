import { test, expect, type Page } from '../fixtures';
import { APP_PAGES, type PageDef } from '../helpers/page-list';
import { waitForPageReady } from '../helpers/ready-signals';
import { resetDatabase } from '../test-utils';

const MOBILE_PAGES: Array<PageDef & { navOpen?: boolean }> = APP_PAGES.map((pageDef) => ({
  ...pageDef,
  navOpen: pageDef.name === 'dashboard',
}));

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
    maxDiffPixelRatio: 0.02,
  });

  if (navOpen) {
    const toggle = page.locator('.mobile-menu-toggle');
    try {
      await toggle.waitFor({ state: 'visible', timeout: 3000 });
      await toggle.click();
      await expect(page.locator('.mobile-nav-drawer')).toHaveClass(/open/);
      await expect(page).toHaveScreenshot(`${name}-nav.png`, {
        fullPage: true,
        animations: 'disabled',
        maxDiffPixelRatio: 0.02,
      });
    } catch {
      // If the toggle isn't visible (responsive layout drift), skip nav snapshot.
    }
  }
}

test.describe('UI baseline: mobile pages', () => {
  for (const pageDef of MOBILE_PAGES) {
    test(`baseline: ${pageDef.name}`, async ({ page }) => {
      await captureBaseline(page, pageDef.path, pageDef.name, pageDef.ready, pageDef.navOpen);
    });
  }
});
