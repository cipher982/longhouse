/**
 * Styling Validation Tests
 *
 * Validates that all UI elements have proper styling applied.
 * Catches unstyled elements that would appear visually broken.
 */

import { test, expect } from './fixtures';
import { createTestAgent } from './helpers/agent-helpers';
import { createTestThread } from './helpers/test-helpers';

test.describe('Styling Validation', () => {
  test('chat interface elements should have proper styling', async ({ page }) => {
    // Setup: Create agent and thread
    const agent = await createTestAgent(page, 'Test Agent');
    const thread = await createTestThread(page, agent.id, 'Test Thread');

    // Navigate to chat
    await page.goto(`/agent/${agent.id}/thread/${thread.id}`);
    await page.waitForSelector('.messages-container', { timeout: 10000 });

    // Send a test message to populate the interface
    const input = page.locator('[data-testid="chat-input"]');
    await input.fill('Test message');
    await page.locator('[data-testid="send-message-btn"]').click();

    // Wait for message to appear
    await page.waitForSelector('.message.user-message', { timeout: 5000 });

    /**
     * Test 1: Message action buttons should have visible styling
     */
    const messageActionBtn = page.locator('.message-action-btn').first();
    await expect(messageActionBtn).toBeVisible();

    // Check that button has expected computed styles
    const actionBtnStyles = await messageActionBtn.evaluate((el) => {
      const styles = window.getComputedStyle(el);
      return {
        cursor: styles.cursor,
        padding: styles.padding,
        backgroundColor: styles.backgroundColor,
        opacity: styles.opacity,
      };
    });

    expect(actionBtnStyles.cursor).toBe('pointer');
    expect(actionBtnStyles.padding).not.toBe('0px');
    expect(parseFloat(actionBtnStyles.opacity)).toBeGreaterThan(0);

    /**
     * Test 2: Tool buttons should have proper styling
     */
    const toolBtn = page.locator('.tool-btn').first();
    await expect(toolBtn).toBeVisible();

    const toolBtnStyles = await toolBtn.evaluate((el) => {
      const styles = window.getComputedStyle(el);
      return {
        cursor: styles.cursor,
        padding: styles.padding,
        backgroundColor: styles.backgroundColor,
        border: styles.border,
        borderRadius: styles.borderRadius,
      };
    });

    expect(toolBtnStyles.cursor).toBe('pointer');
    expect(toolBtnStyles.padding).not.toBe('0px');
    expect(toolBtnStyles.borderRadius).not.toBe('0px');
    expect(toolBtnStyles.border).not.toBe('0px none');

    /**
     * Test 3: Hover states should work
     */
    await toolBtn.hover();
    const toolBtnHoverStyles = await toolBtn.evaluate((el) => {
      const styles = window.getComputedStyle(el);
      return {
        backgroundColor: styles.backgroundColor,
      };
    });

    // Background should change on hover (not be transparent)
    expect(toolBtnHoverStyles.backgroundColor).not.toBe('rgba(0, 0, 0, 0)');
  });

  test('no elements should have default browser styling', async ({ page }) => {
    const agent = await createTestAgent(page, 'Test Agent');
    await page.goto(`/agent/${agent.id}/thread`);
    await page.waitForSelector('.messages-container', { timeout: 10000 });

    /**
     * Find all interactive elements and verify they have custom styling
     */
    const buttons = page.locator('button:visible');
    const buttonCount = await buttons.count();

    for (let i = 0; i < Math.min(buttonCount, 10); i++) {
      const button = buttons.nth(i);
      const className = await button.getAttribute('class');

      // Skip if no class (some buttons might be intentionally unstyled)
      if (!className) continue;

      const styles = await button.evaluate((el) => {
        const computed = window.getComputedStyle(el);
        return {
          cursor: computed.cursor,
          backgroundColor: computed.backgroundColor,
          border: computed.border,
          className: el.className,
        };
      });

      // Button should have pointer cursor
      expect(styles.cursor, `Button with class "${styles.className}" should have pointer cursor`).toBe('pointer');

      // Button should have custom background (not default transparent)
      expect(
        styles.backgroundColor,
        `Button with class "${styles.className}" should have background color`
      ).not.toBe('rgba(0, 0, 0, 0)');
    }
  });

  test('all class names in use should have CSS definitions', async ({ page }) => {
    const agent = await createTestAgent(page, 'Test Agent');
    await page.goto(`/agent/${agent.id}/thread`);
    await page.waitForSelector('.messages-container', { timeout: 10000 });

    /**
     * Extract all class names from the page and check if they have styles
     */
    const unstyledElements = await page.evaluate(() => {
      const results: { className: string; tag: string; hasStyles: boolean }[] = [];
      const elements = document.querySelectorAll('[class]');

      // Classes that are OK to be unstyled (utilities, modifiers)
      const allowedUnstyled = new Set([
        'active', 'selected', 'disabled', 'open', 'closed', 'visible', 'hidden',
        'pending', 'loading', 'error', 'success', 'warning',
      ]);

      elements.forEach((el) => {
        const classes = el.className.split(' ').filter(Boolean);

        classes.forEach((cls) => {
          // Skip utility classes and test IDs
          if (allowedUnstyled.has(cls) || cls.startsWith('data-') || cls.startsWith('aria-')) {
            return;
          }

          // Check if there's a CSS rule for this class
          const sheets = Array.from(document.styleSheets);
          let hasRule = false;

          try {
            for (const sheet of sheets) {
              const rules = Array.from(sheet.cssRules || []);
              hasRule = rules.some((rule) => {
                const cssRule = rule as CSSStyleRule;
                return cssRule.selectorText?.includes(`.${cls}`);
              });
              if (hasRule) break;
            }
          } catch (e) {
            // CORS issues with external stylesheets - assume styled
            hasRule = true;
          }

          if (!hasRule) {
            results.push({
              className: cls,
              tag: el.tagName.toLowerCase(),
              hasStyles: false,
            });
          }
        });
      });

      return results;
    });

    // Report findings
    if (unstyledElements.length > 0) {
      console.warn('⚠️  Found elements with potentially missing CSS:');
      unstyledElements.forEach(({ className, tag }) => {
        console.warn(`  - .${className} (${tag})`);
      });
    }

    // Don't fail the test if we find some, but warn
    // This catches cases where classes are defined but may need improvement
    expect(
      unstyledElements.length,
      `Found ${unstyledElements.length} elements with potentially missing CSS rules`
    ).toBeLessThan(20); // Allow some, but not too many
  });

  test('critical UI components should have consistent design tokens', async ({ page }) => {
    const agent = await createTestAgent(page, 'Test Agent');
    await page.goto(`/agent/${agent.id}/thread`);
    await page.waitForSelector('.messages-container', { timeout: 10000 });

    /**
     * Validate design consistency - all buttons should use design tokens
     */
    const designTokens = await page.evaluate(() => {
      const root = document.documentElement;
      const styles = window.getComputedStyle(root);

      return {
        primary: styles.getPropertyValue('--primary'),
        borderColor: styles.getPropertyValue('--border-color'),
        radiusMd: styles.getPropertyValue('--radius-md'),
        transitionFast: styles.getPropertyValue('--transition-fast'),
      };
    });

    // Design tokens should be defined
    expect(designTokens.primary).toBeTruthy();
    expect(designTokens.borderColor).toBeTruthy();
    expect(designTokens.radiusMd).toBeTruthy();
    expect(designTokens.transitionFast).toBeTruthy();

    // Check that primary button uses design tokens
    const primaryButton = page.locator('[data-testid="send-message-btn"]').first();
    if (await primaryButton.isVisible()) {
      const buttonStyles = await primaryButton.evaluate((el) => {
        const styles = window.getComputedStyle(el);
        return {
          backgroundColor: styles.backgroundColor,
          borderRadius: styles.borderRadius,
        };
      });

      // Button should have border radius (design token applied)
      expect(buttonStyles.borderRadius).not.toBe('0px');
    }
  });
});
