import { testLog } from './test-logger';

import { Page, expect } from '@playwright/test';
import { createApiClient, Automation, Thread, CreateAutomationRequest } from './api-client';
import { resetDatabaseForCommis } from './database-helpers';
import { createAutomationViaAPI, cleanupAutomations } from './automation-helpers';
import { logTestStep } from './test-utils';

export interface TestContext {
  automations: Automation[];
  threads: Thread[];
}

/**
 * Setup helper that creates test data and returns a context object
 */
export async function setupTestData(commisId: string, options: {
  automations?: CreateAutomationRequest[];
  threadsPerAutomation?: number;
} = {}): Promise<TestContext> {
  logTestStep('Setting up test data', { commisId, options });

  const apiClient = createApiClient(commisId);
  const context: TestContext = {
    automations: [],
    threads: []
  };

  const automationConfigs = options.automations || [{}];
  for (const automationConfig of automationConfigs) {
    const automation = await createAutomationViaAPI(commisId, automationConfig);
    context.automations.push(automation);

    const threadCount = options.threadsPerAutomation || 0;
    for (let i = 0; i < threadCount; i++) {
      const thread = await apiClient.createThread({
        fiche_id: automation.id,
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
export async function cleanupTestData(commisId: string, context: TestContext): Promise<void> {
  if (!context) {
    return;
  }

  logTestStep('Cleaning up test data', { commisId, automationCount: context.automations?.length, threadCount: context.threads?.length });

  const apiClient = createApiClient(commisId);

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
    await cleanupAutomations(commisId, context.automations);
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
 * Wait for the dashboard to be ready (app loaded and dashboard rendered)
 */
export async function waitForDashboardReady(page: Page): Promise<void> {
  try {
    await page.goto('/dashboard', { waitUntil: 'networkidle' });

    // Wait for critical UI elements to be interactive
    await Promise.all([
      page.waitForSelector('#dashboard-container:visible', { timeout: 2000 }),
      page.waitForSelector('[data-testid="create-automation-btn"]:not([disabled])', { timeout: 2000 })
    ]);
  } catch (error) {
    // Log detailed error information
    testLog.error('Dashboard failed to load properly:', error);

    // Try to get current DOM state for debugging
    const domState = await page.evaluate(() => ({
      dashboardRoot: !!document.querySelector('#dashboard-root'),
      dashboardContainer: !!document.querySelector('#dashboard-container'),
      dashboard: !!document.querySelector('#dashboard'),
      table: !!document.querySelector('table'),
      createBtn: !!document.querySelector('[data-testid="create-automation-btn"]'),
      bodyHTML: document.body.innerHTML.substring(0, 200)
    }));

    testLog.error('Current DOM state:', domState);
    throw new Error(`Dashboard did not load properly. DOM state: ${JSON.stringify(domState)}`);
  }

  // Wait for data-ready signal instead of arbitrary timeout
  await page.waitForFunction(
    () => document.body.getAttribute('data-ready') === 'true',
    {},
    { timeout: 5000 }
  );
}

/**
 * Get the count of automation rows in the dashboard.
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
  await waitForDashboardReady(page);
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
 * Edit an automation via the UI modal.
 */
export async function editAutomationViaUI(page: Page, automationId: string, data: {
  name?: string;
  systemInstructions?: string;
  taskInstructions?: string;
  temperature?: number;
  model?: string;
}): Promise<void> {
  // Open edit modal
  await page.locator(`[data-testid="edit-automation-${automationId}"]`).click();
  await expect(page.locator('#fiche-modal')).toBeVisible({ timeout: 2000 });
  await page.waitForSelector('#fiche-name:not([disabled])', { timeout: 2000 });

  // Fill form fields
  if (data.name !== undefined) {
    await page.locator('#fiche-name').fill(data.name);
  }

  if (data.systemInstructions !== undefined) {
    await page.locator('#system-instructions').fill(data.systemInstructions);
  }

  if (data.taskInstructions !== undefined) {
    await page.locator('#default-task-instructions').fill(data.taskInstructions);
  }

  if (data.temperature !== undefined) {
    const tempInput = page.locator('#temperature-input');
    if (await tempInput.count() > 0) {
      await tempInput.fill(data.temperature.toString());
    }
  }

  if (data.model !== undefined) {
    const modelSelect = page.locator('#model-select');
    if (await modelSelect.count() > 0) {
      await modelSelect.selectOption(data.model);
    }
  }

  // Save changes
  await page.locator('#save-fiche').click();

  // Wait for modal to close (hidden)
  await expect(page.locator('#fiche-modal')).not.toBeVisible({ timeout: 5000 });
}

/**
 * Delete an automation via the UI and handle the confirmation dialog.
 */
export async function deleteAutomationViaUI(page: Page, automationId: string, confirm: boolean = true): Promise<void> {
  // Set up dialog handler
  page.once('dialog', (dialog) => {
    if (confirm) {
      dialog.accept();
    } else {
      dialog.dismiss();
    }
  });

  // Click delete button
  await page.locator(`[data-testid="delete-automation-${automationId}"]`).click();

  if (confirm) {
    await expect(page.locator(`tr[data-automation-id="${automationId}"]`)).toHaveCount(0, { timeout: 5000 });
  } else {
    await expect(page.locator(`tr[data-automation-id="${automationId}"]`)).toHaveCount(1);
  }
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
 * @deprecated Use resetDatabaseForCommis from database-helpers.ts instead
 */
export async function resetDatabase(commisId: string): Promise<void> {
  logTestStep('Resetting database (deprecated method)', { commisId });
  await resetDatabaseForCommis(commisId);
}

/**
 * Check if the backend is healthy and responding
 */
export async function checkBackendHealth(commisId: string = '0'): Promise<boolean> {
  const apiClient = createApiClient(commisId);
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
  const commisId = process.env.TEST_PARALLEL_INDEX ?? process.env.TEST_WORKER_INDEX ?? '0';
  const apiClient = createApiClient(commisId);

  const thread = await apiClient.createThread({
    fiche_id: automationId,
    title: title || `Test Thread ${Date.now()}`,
  });

  logTestStep(`Test thread created: ${thread.title} (ID: ${thread.id})`);
  return thread;
}
