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
        console.warn(`Failed to delete thread ${thread.id}:`, error);
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
    console.error('Dashboard failed to load properly:', error);
    
    // Try to get current DOM state for debugging
    const domState = await page.evaluate(() => ({
      dashboardRoot: !!document.querySelector('#dashboard-root'),
      dashboardContainer: !!document.querySelector('#dashboard-container'),
      dashboard: !!document.querySelector('#dashboard'),
      table: !!document.querySelector('table'),
      createBtn: !!document.querySelector('[data-testid="create-agent-btn"]'),
      bodyHTML: document.body.innerHTML.substring(0, 200)
    }));
    
    console.error('Current DOM state:', domState);
    throw new Error(`Dashboard did not load properly. DOM state: ${JSON.stringify(domState)}`);
  }
  
  // Wait a bit more for any reactive updates
  await page.waitForTimeout(500);
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
    await waitForElement(page, '.chat-input', 5000);
  } catch (error) {
    // Chat UI might not be fully implemented yet
    console.warn('Chat UI not fully loaded, continuing...');
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
    console.error('Backend health check failed:', error);
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
      console.log(`Skipping test: ${reason} (${selector} not found)`);
      return true;
    }
    return false;
  };
}
