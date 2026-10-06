import { test, expect, type Page } from '../fixtures';
import { APP_PAGES, type PageDef } from '../helpers/page-list';
import { waitForPageReady } from '../helpers/ready-signals';
import { getPlatformScopedSnapshotName, installDeterministicVisualFonts, stubBaselineDeviceTokens, volatileRegions } from '../helpers/visual-baseline';
import { resetDatabase } from '../test-utils';

const MOBILE_VIEWPORTS = [
  {
    name: 'mobile',
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 3,
  },
  {
    name: 'mobile-small',
    viewport: { width: 375, height: 667 },
    deviceScaleFactor: 2,
  },
] as const;

const MOBILE_PAGES: Array<PageDef & { navOpen?: boolean }> = APP_PAGES.map(
  (pageDef) => ({
    ...pageDef,
    navOpen: pageDef.name === 'timeline',
  }),
);

test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

async function waitForAppReady(page: Page, mode: string) {
  if (mode === 'page') {
    await waitForPageReady(page, { timeout: 20000 });
  }
}

async function captureBaseline(
  page: Page,
  path: string,
  name: string,
  ready: string,
  viewportName: string,
  navOpen?: boolean
) {
  await stubBaselineDeviceTokens(page);
  await page.goto(path);
  await installDeterministicVisualFonts(page);
  await waitForAppReady(page, ready);
  await expect(page).toHaveScreenshot(`${getPlatformScopedSnapshotName(`${name}-${viewportName}`)}.png`, {
    fullPage: true,
    animations: 'disabled',
    maxDiffPixelRatio: 0.02,
    mask: volatileRegions(page),
  });

  if (navOpen) {
    // A missing toggle or a differing drawer is a failure, not a skip: the old
    // try/catch also swallowed screenshot mismatches.
    const toggle = page.locator('.mobile-menu-toggle');
    await toggle.click();
    await expect(page.locator('.mobile-nav-drawer')).toHaveClass(/open/);
    await expect(page).toHaveScreenshot(`${getPlatformScopedSnapshotName(`${name}-nav-${viewportName}`)}.png`, {
      fullPage: true,
      animations: 'disabled',
      maxDiffPixelRatio: 0.02,
      mask: volatileRegions(page),
    });
  }
}

for (const viewportDef of MOBILE_VIEWPORTS) {
  test.describe(`UI baseline: ${viewportDef.name} pages`, () => {
    test.use({
      viewport: viewportDef.viewport,
      isMobile: true,
      hasTouch: true,
      deviceScaleFactor: viewportDef.deviceScaleFactor,
    });

    for (const pageDef of MOBILE_PAGES) {
      test(`baseline: ${pageDef.name}`, async ({ page }) => {
        await captureBaseline(
          page,
          pageDef.path,
          pageDef.name,
          pageDef.ready,
          viewportDef.name,
          pageDef.navOpen,
        );
      });
    }
  });
}
