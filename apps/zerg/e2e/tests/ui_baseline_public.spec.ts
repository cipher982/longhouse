import { test, expect, type Page } from './fixtures';

const PUBLIC_PAGES = [
  { name: 'landing', path: '/landing?clock=frozen&fx=none' },
  { name: 'pricing', path: '/pricing?clock=frozen' },
  { name: 'docs', path: '/docs?clock=frozen' },
  { name: 'changelog', path: '/changelog?clock=frozen' },
  { name: 'privacy', path: '/privacy?clock=frozen' },
  { name: 'security', path: '/security?clock=frozen' },
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
