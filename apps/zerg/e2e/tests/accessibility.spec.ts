import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

test.describe('Accessibility – dashboard smoke', () => {
  test('dashboard view has no serious axe violations', async ({ page }) => {
    await page.goto('/');

    // Run axe accessibility checks
    const accessibilityScanResults = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa'])
      .analyze();

    // Filter for only critical and serious violations
    const seriousViolations = accessibilityScanResults.violations.filter(
      (violation) => violation.impact === 'critical' || violation.impact === 'serious'
    );

    // Log violations for debugging if any exist
    if (seriousViolations.length > 0) {
      console.log('Accessibility violations found:', JSON.stringify(seriousViolations, null, 2));
    }

    expect(seriousViolations).toEqual([]);
  });
});
