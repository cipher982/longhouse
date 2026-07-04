import { testLog } from './test-logger';

import { Page, expect } from '@playwright/test';
import { createApiClient, Automation, Thread, CreateAutomationRequest } from './api-client';
import { resetDatabaseForWorker } from './database-helpers';
import { createAutomationViaAPI, cleanupAutomations } from './automation-helpers';
import { logTestStep } from './test-utils';

export interface TestContext {
  automations: Automation[];
  threads: Thread[];
}

/**
 * Setup helper that creates test data and returns a context object
 */
export async function setupTestData(workerId: string, options: {
  automations?: CreateAutomationRequest[];
  threadsPerAutomation?: number;
} = {}): Promise<TestContext> {
  logTestStep('Setting up test data', { workerId, options });

  const apiClient = createApiClient(workerId);
  const context: TestContext = {
    automations: [],
    threads: []
  };

  const automationConfigs = options.automations || [{}];
  for (const automationConfig of automationConfigs) {
    const automation = await createAutomationViaAPI(workerId, automationConfig);
    context.automations.push(automation);

    const threadCount = options.threadsPerAutomation || 0;
    for (let i = 0; i < threadCount; i++) {
      const thread = await apiClient.createThread({
        automation_id: automation.id,
        title: `Test Thread ${i + 1} for ${automation.name}`
      });
      context.threads.push(thread);
    }
  }

  logTestStep('Test data setup complete', { automationCount: context.automations.length, threadCount: context.threads.length });
  return context;
}

/**
 * Cleanup helper that removes test data
 */
export async function cleanupTestData(workerId: string, context: TestContext): Promise<void> {
  if (!context) {
    return;
  }

  logTestStep('Cleaning up test data', { workerId, automationCount: context.automations?.length, threadCount: context.threads?.length });

  const apiClient = createApiClient(workerId);

  if (context.threads) {
    for (const thread of context.threads) {
      try {
        await apiClient.deleteThread(thread.id);
      } catch (error) {
        testLog.warn(`Failed to delete thread ${thread.id}:`, error);
      }
    }
  }

  if (context.automations) {
    await cleanupAutomations(workerId, context.automations);
  }

  logTestStep('Test data cleanup complete');
}

/**
 * Wait for an element to be visible with a custom error message
 */
export async function waitForElement(page: Page, selector: string, timeout: number = 10000): Promise<void> {
  try {
    await page.waitForSelector(selector, { state: 'visible', timeout });
  } catch (error) {
    throw new Error(`Element "${selector}" not found within ${timeout}ms. Current URL: ${page.url()}`);
  }
}

/**
 * Wait for the automations overview to be ready.
 */
export async function waitForAutomationsReady(page: Page): Promise<void> {
  try {
    await page.goto('/automations', { waitUntil: 'networkidle' });

    // Wait for critical UI elements to be interactive
    await Promise.all([
      page.waitForSelector('#automations-container:visible', { timeout: 2000 }),
      page.waitForSelector('[data-testid="create-automation-btn"]:not([disabled])', { timeout: 2000 })
    ]);
  } catch (error) {
    // Log detailed error information
    testLog.error('Automations failed to load properly:', error);

    // Try to get current DOM state for debugging
    const domState = await page.evaluate(() => ({
      automationsContainer: !!document.querySelector('#automations-container'),
      table: !!document.querySelector('table'),
      createBtn: !!document.querySelector('[data-testid="create-automation-btn"]'),
      bodyHTML: document.body.innerHTML.substring(0, 200)
    }));

    testLog.error('Current DOM state:', domState);
    throw new Error(`Automations did not load properly. DOM state: ${JSON.stringify(domState)}`);
  }

  // Wait for data-ready signal instead of arbitrary timeout
  await page.waitForFunction(
    () => document.body.getAttribute('data-ready') === 'true',
    {},
    { timeout: 5000 }
  );
}

/**
 * Get the count of automation rows in the overview table.
 */
export async function getAutomationRowCount(page: Page): Promise<number> {
  await page.waitForLoadState('networkidle');
  return await page.locator('tr[data-automation-id]:visible').count();
}

/**
 * Create an automation via the UI and return its ID.
 * CRITICAL: Gets the ID from the API response, not from the DOM.
 */
export async function createAutomationViaUI(page: Page): Promise<string> {
  await waitForAutomationsReady(page);
  const createBtn = page.locator('[data-testid="create-automation-btn"]');
  await expect(createBtn).toBeVisible({ timeout: 10000 });
  await expect(createBtn).toBeEnabled({ timeout: 5000 });

  // Capture the API response to get the actual created automation ID.
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/api/automations') && r.request().method() === 'POST' && r.status() === 201,
      { timeout: 10000 }
    ),
    createBtn.click(),
  ]);

  // Parse the automation ID from the response body. This is deterministic.
  const body = await response.json();
  const automationId = String(body.id);

  if (!automationId || automationId === 'undefined') {
    throw new Error(`Failed to get automation ID from API response: ${JSON.stringify(body)}`);
  }

  const newRow = page.locator(`tr[data-automation-id="${automationId}"]`);
  await expect(newRow).toBeVisible({ timeout: 10000 });

  return automationId;
}

/**
 * Navigate to chat for a specific automation.
 */
export async function navigateToChat(page: Page, automationId: string): Promise<void> {
  await page.locator(`[data-testid="chat-automation-${automationId}"]`).click();

  // Wait for chat interface to load (if implemented)
  try {
    await waitForElement(page, '[data-testid="chat-input"]', 5000);
  } catch (error) {
    // Chat UI might not be fully implemented yet
    testLog.warn('Chat UI not fully loaded, continuing...');
  }
}

/**
 * Reset the database to a clean state
 * @deprecated Use resetDatabaseForWorker from database-helpers.ts instead
 */
export async function resetDatabase(workerId: string): Promise<void> {
  logTestStep('Resetting database (deprecated method)', { workerId });
  await resetDatabaseForWorker(workerId);
}

/**
 * Check if the backend is healthy and responding
 */
export async function checkBackendHealth(workerId: string = '0'): Promise<boolean> {
  const apiClient = createApiClient(workerId);
  try {
    const response = await apiClient.healthCheck();
    return response && response.message === 'Longhouse API is running';
  } catch (error) {
    testLog.error('Backend health check failed:', error);
    return false;
  }
}

/**
 * Skip test if a UI element is not implemented
 */
export function skipIfNotImplemented(page: Page, selector: string, reason: string = 'UI not implemented yet') {
  return async function() {
    const count = await page.locator(selector).count();
    if (count === 0) {
      testLog.info(`Skipping test: ${reason} (${selector} not found)`);
      return true;
    }
    return false;
  };
}

/**
 * Toast notification helpers for react-hot-toast
 *
 * React-hot-toast renders with:
 * - .toast - base class for all toasts
 * - .toast-success - success toasts
 * - .toast-error - error toasts (may not exist, check .toast instead)
 * - role="status" or role="alert" depending on type
 *
 * @example
 * // Wait for any toast containing text
 * await waitForToast(page, 'Settings saved');
 *
 * // Wait for success toast
 * const toast = getToastLocator(page, { type: 'success', text: 'Automation created' });
 * await expect(toast).toBeVisible();
 */

/**
 * Get a locator for a toast notification
 */
export function getToastLocator(page: Page, options?: {
  type?: 'success' | 'error' | 'any';
  text?: string;
}) {
  const { type = 'any', text } = options || {};

  // Build base selector without text constraint
  let baseSelector = '.toast';
  if (type === 'success') {
    baseSelector = '.toast-success, .toast'; // Fallback to .toast if type class doesn't exist
  } else if (type === 'error') {
    // react-hot-toast may not have .toast-error, just use .toast with text match
    baseSelector = '.toast';
  }

  // Get base locator
  const baseLocator = page.locator(baseSelector);

  // Apply text filter using .filter() to ensure it applies to ALL matched elements
  // This avoids the comma-separated selector bug where text only applies to last branch
  if (text) {
    return baseLocator.filter({ hasText: text });
  }

  return baseLocator;
}

/**
 * Wait for a toast notification to appear
 */
export async function waitForToast(
  page: Page,
  text: string,
  options?: {
    timeout?: number;
    type?: 'success' | 'error' | 'any';
  }
) {
  const { timeout = 3000, type = 'any' } = options || {};
  const toast = getToastLocator(page, { type, text });
  await expect(toast).toBeVisible({ timeout });
  return toast;
}

/**
 * Create a test thread using the API client
 * This is a convenience wrapper for tests that have a Page but need to create threads
 */
export async function createTestThread(page: Page, automationId: string, title: string): Promise<Thread> {
  const workerId = process.env.TEST_PARALLEL_INDEX ?? process.env.TEST_WORKER_INDEX ?? '0';
  const apiClient = createApiClient(workerId);

  const thread = await apiClient.createThread({
    automation_id: automationId,
    title: title || `Test Thread ${Date.now()}`,
  });

  logTestStep(`Test thread created: ${thread.title} (ID: ${thread.id})`);
  return thread;
}
