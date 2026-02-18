import { test, expect } from './fixtures';
import AxeBuilder from '@axe-core/playwright';

/**
 * Accessibility tests using axe-core.
 *
 * Runs WCAG 2.0 AA checks including color contrast on key pages.
 * This is the canonical accessibility test — axe-core computes real
 * contrast ratios from rendered styles in the browser DOM.
 */

const PAGES_TO_TEST = [
  { name: 'landing', path: '/', needsAuth: false },
  { name: 'dashboard', path: '/dashboard', needsAuth: true },
  { name: 'timeline', path: '/timeline', needsAuth: true },
  { name: 'settings', path: '/settings', needsAuth: true },
];

test.describe('Accessibility – axe-core WCAG AA', () => {
  for (const pageDef of PAGES_TO_TEST) {
    test(`${pageDef.name} has no serious axe violations`, async ({ page }) => {
      await page.goto(pageDef.path);
      await page.waitForLoadState('domcontentloaded');

      // Wait for app content to render
      await page.waitForFunction(
        () => document.querySelector('[data-testid="app-container"], .landing-page') !== null,
        { timeout: 10000 }
      ).catch(() => {
        // Landing page may not have these markers — proceed anyway
      });

      const results = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa'])
        .analyze();

      const seriousViolations = results.violations.filter(
        (violation) => violation.impact === 'critical' || violation.impact === 'serious'
      );

      if (seriousViolations.length > 0) {
        const summary = seriousViolations.map(v =>
          `[${v.impact}] ${v.id}: ${v.description} (${v.nodes.length} instances)`
        ).join('\n');
        console.log(`Accessibility violations on ${pageDef.name}:\n${summary}`);
      }

      expect(seriousViolations).toEqual([]);
    });
  }
});
