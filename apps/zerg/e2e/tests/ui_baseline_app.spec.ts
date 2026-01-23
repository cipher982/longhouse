import { test, expect, type Page } from './fixtures';
import { waitForPageReady } from './helpers/ready-signals';

const BASE_QUERY = 'clock=frozen&effects=off&seed=ui-baseline';

const APP_PAGES = [
  { name: 'dashboard', path: `/dashboard?${BASE_QUERY}`, ready: 'page' },
  { name: 'chat', path: `/chat?${BASE_QUERY}`, ready: 'page' },
  { name: 'canvas', path: `/canvas?${BASE_QUERY}`, ready: 'page' },
  { name: 'settings', path: `/settings?${BASE_QUERY}`, ready: 'settings' },
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
