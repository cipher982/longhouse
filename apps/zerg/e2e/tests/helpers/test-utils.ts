import { testLog } from './test-logger';

import { Page, TestInfo } from '@playwright/test';

/**
 * Common test utilities and patterns for E2E tests
 * Consolidates repetitive code and provides consistent behavior
 */

export interface RetryOptions {
  maxAttempts?: number;
  delayMs?: number;
  exponentialBackoff?: boolean;
}

export interface WaitOptions {
  timeout?: number;
  pollInterval?: number;
  errorMessage?: string;
}

/**
 * Get worker ID from test context (standardized approach)
 */
export function getWorkerIdFromTest(testInfo: TestInfo): string {
  return String(testInfo.workerIndex);
}

/**
 * Generic retry function with exponential backoff
 */
export async function retryWithBackoff<T>(
  fn: () => Promise<T>,
  options: RetryOptions = {}
): Promise<T> {
  const { maxAttempts = 3, delayMs = 1000, exponentialBackoff = false } = options;

  let attempts = 0;
  let lastError: Error;

  while (attempts < maxAttempts) {
    try {
      return await fn();
    } catch (error) {
      lastError = error as Error;
      attempts++;

      if (attempts >= maxAttempts) {
        throw new Error(`Operation failed after ${maxAttempts} attempts. Last error: ${lastError.message}`);
      }

      const delay = exponentialBackoff ? delayMs * Math.pow(2, attempts - 1) : delayMs;
      await new Promise(resolve => setTimeout(resolve, delay));
    }
  }

  throw lastError!;
}

/**
 * Wait for element to be stable (not changing size/position)
 */
export async function waitForStableElement(
  page: Page,
  selector: string,
  options: WaitOptions = {}
): Promise<void> {
  const { timeout = 10000, pollInterval = 100 } = options;
  const startTime = Date.now();

  let lastRect: { width: number; height: number; x: number; y: number } | null = null;
  let stableCount = 0;
  const requiredStableCount = 3; // Element must be stable for 3 consecutive checks

  while (Date.now() - startTime < timeout) {
    try {
      const element = page.locator(selector);
      await element.waitFor({ state: 'visible', timeout: 1000 });

      const rect = await element.boundingBox();
      if (!rect) {
        throw new Error('Element has no bounding box');
      }

      if (lastRect &&
          rect.width === lastRect.width &&
          rect.height === lastRect.height &&
          rect.x === lastRect.x &&
          rect.y === lastRect.y) {
        stableCount++;

        if (stableCount >= requiredStableCount) {
          return; // Element is stable
        }
      } else {
        stableCount = 0; // Reset stability count
      }

      lastRect = rect;
      await new Promise(resolve => setTimeout(resolve, pollInterval));
    } catch (error) {
      await new Promise(resolve => setTimeout(resolve, pollInterval));
    }
  }

  throw new Error(`Element "${selector}" did not become stable within ${timeout}ms`);
}

/**
 * Skip test if UI element is not implemented
 */
export async function skipIfNotImplemented(
  page: Page,
  selector: string,
  reason: string = 'UI not implemented yet'
): Promise<boolean> {
  try {
    const count = await page.locator(selector).count();
    if (count === 0) {
      testLog.info(`⏭️  Skipping test: ${reason} (${selector} not found)`);
      return true;
    }
    return false;
  } catch (error) {
    testLog.info(`⏭️  Skipping test: ${reason} (error checking ${selector})`);
    return true;
  }
}

/**
 * Wait for network to be idle (no pending requests)
 */
export async function waitForNetworkIdle(
  page: Page,
  options: { timeout?: number; idleTime?: number } = {}
): Promise<void> {
  const { timeout = 30000, idleTime = 500 } = options;

  await page.waitForLoadState('networkidle', { timeout });

  // Additional wait for any delayed requests
  await new Promise(resolve => setTimeout(resolve, idleTime));
}

/**
 * Safe navigation with retry logic
 */
export async function safeNavigate(
  page: Page,
  url: string,
  options: { retries?: number; waitUntil?: 'networkidle' | 'load' | 'domcontentloaded' } = {}
): Promise<void> {
  const { retries = 3, waitUntil = 'networkidle' } = options;

  await retryWithBackoff(async () => {
    await page.goto(url, { waitUntil });
  }, { maxAttempts: retries });
}

/**
 * Safe click with retry logic for flaky elements
 */
export async function safeClick(
  page: Page,
  selector: string,
  options: RetryOptions & { waitFor?: 'visible' | 'attached' } = {}
): Promise<void> {
  const { waitFor = 'visible', ...retryOptions } = options;

  await retryWithBackoff(async () => {
    const element = page.locator(selector);
    await element.waitFor({ state: waitFor, timeout: 5000 });
    await element.click();
  }, retryOptions);
}

/**
 * Safe fill with retry logic
 */
export async function safeFill(
  page: Page,
  selector: string,
  value: string,
  options: RetryOptions = {}
): Promise<void> {
  await retryWithBackoff(async () => {
    const element = page.locator(selector);
    await element.waitFor({ state: 'visible', timeout: 5000 });
    await element.fill(value);
  }, options);
}

/**
 * Wait for element to contain specific text
 */
export async function waitForText(
  page: Page,
  selector: string,
  expectedText: string,
  options: WaitOptions = {}
): Promise<void> {
  const { timeout = 10000, errorMessage } = options;

  await page.waitForFunction(
    ({ selector, expectedText }) => {
      const element = document.querySelector(selector);
      return element && element.textContent?.includes(expectedText);
    },
    { selector, expectedText },
    { timeout }
  );
}

/**
 * Get element text with retry logic
 */
export async function getTextSafely(
  page: Page,
  selector: string,
  options: RetryOptions = {}
): Promise<string> {
  return await retryWithBackoff(async () => {
    const element = page.locator(selector);
    await element.waitFor({ state: 'visible', timeout: 5000 });
    const text = await element.textContent();
    if (text === null) {
      throw new Error(`Element "${selector}" has no text content`);
    }
    return text;
  }, options);
}

/**
 * Check if element exists without throwing
 */
export async function elementExists(page: Page, selector: string): Promise<boolean> {
  try {
    const count = await page.locator(selector).count();
    return count > 0;
  } catch (error) {
    return false;
  }
}

/**
 * Wait for multiple elements to be visible
 */
export async function waitForMultipleElements(
  page: Page,
  selectors: string[],
  timeout: number = 10000
): Promise<void> {
  const promises = selectors.map(selector =>
    page.waitForSelector(selector, { state: 'visible', timeout })
  );

  await Promise.all(promises);
}

/**
 * Create a consistent timeout based on test environment
 */
export function getTestTimeout(base: number = 5000): number {
  // Increase timeout in CI or when running in headed mode
  const isCI = process.env.CI === 'true';
  const isHeaded = process.env.HEADED === 'true';

  let multiplier = 1;
  if (isCI) multiplier *= 2;
  if (isHeaded) multiplier *= 1.5;

  return Math.round(base * multiplier);
}

/**
 * Log test step with consistent formatting
 */
export function logTestStep(step: string, details?: any): void {
  const timestamp = new Date().toISOString().split('T')[1].split('.')[0];
  testLog.info(`📊 [${timestamp}] ${step}`);
  if (details) {
    testLog.info(`   ${JSON.stringify(details)}`);
  }
}

/**
 * Create a test data cleanup function
 */
export function createCleanupFunction(cleanupTasks: (() => Promise<void>)[]): () => Promise<void> {
  return async () => {
    for (const task of cleanupTasks) {
      try {
        await task();
      } catch (error) {
        testLog.warn('Cleanup task failed:', error);
      }
    }
  };
}
