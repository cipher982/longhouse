import { test, expect, type Page } from './fixtures';

const BASE_QUERY = 'clock=frozen&effects=off&seed=ui-baseline';

const PUBLIC_PAGES = [
  { name: 'landing', path: `/landing?${BASE_QUERY}&fx=none` },
  { name: 'pricing', path: `/pricing?${BASE_QUERY}` },
  { name: 'docs', path: `/docs?${BASE_QUERY}` },
  { name: 'changelog', path: `/changelog?${BASE_QUERY}` },
  { name: 'privacy', path: `/privacy?${BASE_QUERY}` },
  { name: 'security', path: `/security?${BASE_QUERY}` },
];

async function captureBaseline(page: Page, path: string, name: string) {
  await page.goto(path);
  await page.waitForLoadState('networkidle');
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
