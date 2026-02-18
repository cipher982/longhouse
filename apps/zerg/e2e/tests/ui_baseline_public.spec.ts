import { test, expect, type Page } from './fixtures';
import { PUBLIC_PAGES, type PageDef } from './helpers/page-list';

async function captureBaseline(page: Page, path: string, name: string) {
  await page.goto(path);
  await page.waitForLoadState('domcontentloaded');
  await expect(page).toHaveScreenshot(`${name}.png`, {
    fullPage: true,
    animations: 'disabled',
  });
}

test.describe('UI baseline: public pages', () => {
  for (const pageDef of PUBLIC_PAGES) {
    test(`baseline: ${pageDef.name}`, async ({ page }) => {
      await captureBaseline(page, pageDef.path, pageDef.name);
    });
  }
});
