import { testLog } from './test-logger';

import { Page, expect } from '@playwright/test';
import { createApiClient, Agent, Thread, CreateAgentRequest, CreateThreadRequest } from './api-client';
import { resetDatabaseForWorker } from './database-helpers';
import { createAgentViaAPI, createMultipleAgents, cleanupAgents } from './agent-helpers';
import { retryWithBackoff, waitForStableElement, logTestStep } from './test-utils';

export interface TestContext {
  agents: Agent[];
  threads: Thread[];
}

/**
 * Setup helper that creates test data and returns a context object
 */
export async function setupTestData(workerId: string, options: {
  agents?: CreateAgentRequest[];
  threadsPerAgent?: number;
} = {}): Promise<TestContext> {
  logTestStep('Setting up test data', { workerId, options });

  const apiClient = createApiClient(workerId);
  const context: TestContext = {
    agents: [],
    threads: []
  };

  // Create agents using the consolidated helper
  const agentConfigs = options.agents || [{}]; // Default to one agent
  for (const agentConfig of agentConfigs) {
    const agent = await createAgentViaAPI(workerId, agentConfig);
    context.agents.push(agent);

    // Create threads for this agent
    const threadCount = options.threadsPerAgent || 0;
    for (let i = 0; i < threadCount; i++) {
      const thread = await apiClient.createThread({
        agent_id: agent.id,
        title: `Test Thread ${i + 1} for ${agent.name}`
      });
      context.threads.push(thread);
    }
  }

  logTestStep('Test data setup complete', { agentCount: context.agents.length, threadCount: context.threads.length });
  return context;
}

/**
 * Cleanup helper that removes test data
 */
export async function cleanupTestData(workerId: string, context: TestContext): Promise<void> {
  if (!context) {
    return;
  }

  logTestStep('Cleaning up test data', { workerId, agentCount: context.agents?.length, threadCount: context.threads?.length });

  const apiClient = createApiClient(workerId);

  // Delete threads first (they reference agents)
  if (context.threads) {
    for (const thread of context.threads) {
      try {
        await apiClient.deleteThread(thread.id);
      } catch (error) {
        testLog.warn(`Failed to delete thread ${thread.id}:`, error);
      }
    }
  }

  // Delete agents using the consolidated helper
  if (context.agents) {
    await cleanupAgents(workerId, context.agents);
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
    await page.goto('/', { waitUntil: 'networkidle' });

    // Wait for critical UI elements to be interactive
    await Promise.all([
      page.waitForSelector('#dashboard:visible', { timeout: 2000 }),
      page.waitForSelector('[data-testid="create-agent-btn"]:not([disabled])', { timeout: 2000 })
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
      createBtn: !!document.querySelector('[data-testid="create-agent-btn"]'),
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
 * Get the count of agent rows in the dashboard
 */
export async function getAgentRowCount(page: Page): Promise<number> {
  await page.waitForLoadState('networkidle');
  return await page.locator('tr[data-agent-id]:visible').count();
}

/**
 * Create an agent via the UI and return its ID
 */
export async function createAgentViaUI(page: Page): Promise<string> {
  await page.locator('[data-testid="create-agent-btn"]').click();

  // Wait for new row to be visible and stable
  const newRow = page.locator('tr[data-agent-id]').first();
  await expect(newRow).toBeVisible({ timeout: 2000 });
  await page.waitForFunction(
    (selector) => {
      const row = document.querySelector(selector);
      return row && row.clientHeight > 0 && row.clientWidth > 0;
    },
    'tr[data-agent-id]:first-of-type'
  );

  const agentId = await newRow.getAttribute('data-agent-id');
  if (!agentId) {
    throw new Error('Failed to get agent ID from newly created agent row');
  }

  return agentId;
}

/**
 * Edit an agent via the UI modal
 */
export async function editAgentViaUI(page: Page, agentId: string, data: {
  name?: string;
  systemInstructions?: string;
  taskInstructions?: string;
  temperature?: number;
  model?: string;
}): Promise<void> {
  // Open edit modal
  await page.locator(`[data-testid="edit-agent-${agentId}"]`).click();
  await expect(page.locator('#agent-modal')).toBeVisible({ timeout: 2000 });
  await page.waitForSelector('#agent-name:not([disabled])', { timeout: 2000 });

  // Fill form fields
  if (data.name !== undefined) {
    await page.locator('#agent-name').fill(data.name);
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
  await page.locator('#save-agent').click();

  // Wait for modal to close (hidden)
  await expect(page.locator('#agent-modal')).not.toBeVisible({ timeout: 5000 });
}

/**
 * Delete an agent via the UI and handle confirmation dialog
 */
export async function deleteAgentViaUI(page: Page, agentId: string, confirm: boolean = true): Promise<void> {
  // Set up dialog handler
  page.once('dialog', (dialog) => {
    if (confirm) {
      dialog.accept();
    } else {
      dialog.dismiss();
    }
  });

  // Click delete button
  await page.locator(`[data-testid="delete-agent-${agentId}"]`).click();

  if (confirm) {
    // Wait for row to disappear
    await expect(page.locator(`tr[data-agent-id="${agentId}"]`)).toHaveCount(0, { timeout: 5000 });
  } else {
    // Row should still be present
    await expect(page.locator(`tr[data-agent-id="${agentId}"]`)).toHaveCount(1);
  }
}

/**
 * Navigate to chat for a specific agent
 */
export async function navigateToChat(page: Page, agentId: string): Promise<void> {
  await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

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
    return response && response.message === 'Agent Platform API is running';
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
 * const toast = getToastLocator(page, { type: 'success', text: 'Agent created' });
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
export async function createTestThread(page: Page, agentId: string, title: string): Promise<Thread> {
  const workerId = process.env.TEST_PARALLEL_INDEX ?? process.env.TEST_WORKER_INDEX ?? '0';
  const apiClient = createApiClient(workerId);

  const thread = await apiClient.createThread({
    agent_id: agentId,
    title: title || `Test Thread ${Date.now()}`,
  });

  logTestStep(`Test thread created: ${thread.title} (ID: ${thread.id})`);
  return thread;
}
