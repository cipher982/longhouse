import { test, expect } from '@playwright/test';
import { injectAxe, checkA11y } from '@axe-core/playwright';

test.describe('Accessibility – dashboard smoke', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await injectAxe(page);
  });

  test('dashboard view has no serious axe violations', async ({ page }) => {
    // Limit to serious / critical for CI signal
    await checkA11y(page, undefined, {
      detailedReport: true,
      axeOptions: {
        runOnly: {
          type: 'tag',
          values: ['wcag2a', 'wcag2aa'],
        },
        resultTypes: ['violations'],
        reporter: 'v2',
      },
      violations: ['critical', 'serious'],
    });
  });
});
