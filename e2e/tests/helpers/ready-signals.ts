/**
 * Ready Signals - E2E test helpers for deterministic page/component readiness
 *
 * These helpers replace arbitrary waitForTimeout() calls with event-driven waiting,
 * making tests more reliable and faster.
 *
 * == READINESS CONTRACT ==
 *
 * All pages follow a unified readiness contract (see frontend-web/src/lib/readiness-contract.ts):
 *
 * 1. data-ready="true" on document.body
 *    Meaning: Page is INTERACTIVE - can click, type, interact
 *    When set: After initial data loaded AND UI is mounted and responsive
 *    Use: waitForPageReady() for most E2E tests
 *
 * 2. data-screenshot-ready="true" on document.body
 *    Meaning: Content is loaded and animations have settled
 *    When set: When visual content is stable for screenshots
 *    Use: waitForScreenshotReady() for marketing automation
 *
 * == RECOMMENDED PATTERNS ==
 *
 * For interactive readiness (most tests):
 *   await waitForPageReady(page);
 *
 * For marketing screenshots:
 *   await waitForScreenshotReady(page);
 */

import { Page } from '@playwright/test';

export interface WaitForPageReadyOptions {
  /** Timeout in milliseconds (default: 10000) */
  timeout?: number;
  /** Attribute name to check (default: 'data-ready') */
  attribute?: string;
  /** Expected attribute value (default: 'true') */
  value?: string;
}

/**
 * Wait for page to signal readiness via data-ready attribute on body.
 *
 * Pages/components set document.body.setAttribute('data-ready', 'true') when they're
 * fully interactive and ready for testing.
 *
 * @example
 * await waitForPageReady(page);
 * // Now safe to interact with the page
 */
export async function waitForPageReady(
  page: Page,
  options: WaitForPageReadyOptions = {}
): Promise<void> {
  const {
    timeout = 10000,
    attribute = 'data-ready',
    value = 'true'
  } = options;

  await page.waitForFunction(
    ({ attr, val }) => document.body.getAttribute(attr) === val,
    { attr: attribute, val: value },
    { timeout }
  );
}

/**
 * Wait for page to be ready for marketing screenshots.
 *
 * This waits for data-screenshot-ready="true" which indicates:
 * - Content is loaded (messages visible, data fetched)
 * - Animations have settled
 * - Visual state is stable for capture
 *
 * Use this for marketing automation, not for interactive E2E tests.
 * For interactive tests, use waitForPageReady() instead.
 *
 * @example
 * await waitForScreenshotReady(page);
 * await page.screenshot({ path: 'marketing-chat.png' });
 */
export async function waitForScreenshotReady(
  page: Page,
  options: { timeout?: number } = {}
): Promise<void> {
  const { timeout = 10000 } = options;

  await page.waitForFunction(
    () => document.body.getAttribute('data-screenshot-ready') === 'true',
    {},
    { timeout }
  );
}

/**
 * Check if a page/component is ready without waiting.
 *
 * Useful for conditional logic or polling scenarios.
 *
 * @example
 * if (await isPageReady(page)) {
 *   // Page is ready, proceed
 * }
 */
export async function isPageReady(
  page: Page,
  options: Omit<WaitForPageReadyOptions, 'timeout'> = {}
): Promise<boolean> {
  const { attribute = 'data-ready', value = 'true' } = options;

  return await page.evaluate(
    ({ attr, val }) => document.body.getAttribute(attr) === val,
    { attr: attribute, val: value }
  );
}

/**
 * Check if page is ready for screenshot capture without waiting.
 *
 * @example
 * if (await isScreenshotReady(page)) {
 *   await page.screenshot({ path: 'capture.png' });
 * }
 */
export async function isScreenshotReady(page: Page): Promise<boolean> {
  return await page.evaluate(
    () => document.body.getAttribute('data-screenshot-ready') === 'true'
  );
}
