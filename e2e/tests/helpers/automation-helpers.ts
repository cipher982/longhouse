import { testLog } from './test-logger';

import { Page, expect } from '@playwright/test';
import { createApiClient, Automation, CreateAutomationRequest } from './api-client';

/**
 * Automation lifecycle helpers for E2E tests.
 * Provides consistent patterns for automation creation, management, and cleanup.
 */

export interface AutomationCreationOptions {
  name?: string;
  model?: string;
  systemInstructions?: string;
  taskInstructions?: string;
  temperature?: number;
  retries?: number;
}

export interface AutomationBatchOptions {
  count: number;
  namePrefix?: string;
  model?: string;
  systemInstructions?: string;
  taskInstructions?: string;
}

/**
 * Create a single automation via API with sensible defaults.
 */
export async function createAutomationViaAPI(
  workerId: string,
  options: AutomationCreationOptions = {}
): Promise<Automation> {
  const apiClient = createApiClient(workerId);

  const config: CreateAutomationRequest = {
    name: options.name || `Test Automation ${workerId}`,
    model: options.model || 'deepseek/deepseek-v4-flash',
    system_instructions: options.systemInstructions || 'You are a test automation for E2E testing',
    task_instructions: options.taskInstructions || 'Perform test tasks as requested',
    temperature: options.temperature || 0.7,
  };

  const retries = options.retries || 3;
  let attempts = 0;

  while (attempts < retries) {
    try {
      const automation = await apiClient.createAutomation(config);
      testLog.info(`Automation created via API: ${automation.name} (ID: ${automation.id})`);
      return automation;
    } catch (error) {
      attempts++;
      testLog.warn(`Automation creation attempt ${attempts} failed:`, error);

      if (attempts >= retries) {
        throw new Error(`Failed to create automation after ${retries} attempts: ${error}`);
      }

      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  }

  throw new Error('Unexpected error in automation creation');
}

/**
 * Create multiple automations in batch.
 */
export async function createMultipleAutomations(
  workerId: string,
  options: AutomationBatchOptions
): Promise<Automation[]> {
  const automations: Automation[] = [];
  const namePrefix = options.namePrefix || 'Batch Automation';

  for (let i = 0; i < options.count; i++) {
    const automation = await createAutomationViaAPI(workerId, {
      name: `${namePrefix} ${i + 1}`,
      model: options.model,
      systemInstructions: options.systemInstructions,
      taskInstructions: options.taskInstructions,
    });
    automations.push(automation);
  }

  testLog.info(`Created ${automations.length} automations in batch`);
  return automations;
}

/**
 * Create an automation via UI and return its ID.
 * CRITICAL: Gets the ID from the API response, not from the DOM.
 */
export async function createAutomationViaUI(page: Page): Promise<string> {
  // Navigate to automations (root redirects to timeline in auth-disabled mode)
  await page.goto('/automations');

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

  // Wait for this specific automation row to appear, not just any row.
  const newRow = page.locator(`tr[data-automation-id="${automationId}"]`);
  await expect(newRow).toBeVisible({ timeout: 10000 });

  testLog.info(`Automation created via UI with ID: ${automationId}`);
  return automationId;
}

/**
 * Get an automation by ID with retry logic.
 */
export async function getAutomationById(workerId: string, automationId: string): Promise<Automation | null> {
  const apiClient = createApiClient(workerId);

  try {
    const automations = await apiClient.listAutomations();
    return automations.find(automation => automation.id === automationId) || null;
  } catch (error) {
    testLog.warn(`Failed to get automation ${automationId}:`, error);
    return null;
  }
}

/**
 * Verify an automation exists and has expected properties.
 */
export async function verifyAutomationExists(
  workerId: string,
  automationId: string,
  expectedName?: string
): Promise<boolean> {
  const automation = await getAutomationById(workerId, automationId);

  if (!automation) {
    testLog.warn(`Automation ${automationId} not found`);
    return false;
  }

  if (expectedName && automation.name !== expectedName) {
    testLog.warn(`Automation ${automationId} has name "${automation.name}", expected "${expectedName}"`);
    return false;
  }

  return true;
}

/**
 * Wait for an automation to appear in the UI.
 */
export async function waitForAutomationInUI(page: Page, automationId: string, timeout: number = 10000): Promise<void> {
  await expect(page.locator(`tr[data-automation-id="${automationId}"]`)).toBeVisible({ timeout });
}

/**
 * Delete an automation via API.
 */
export async function deleteAutomationViaAPI(workerId: string, automationId: string): Promise<void> {
  const apiClient = createApiClient(workerId);

  try {
    await apiClient.deleteAutomation(automationId);
    testLog.info(`Automation ${automationId} deleted via API`);
  } catch (error) {
    testLog.warn(`Failed to delete automation ${automationId}:`, error);
    throw error;
  }
}

/**
 * Cleanup multiple automations.
 */
export async function cleanupAutomations(workerId: string, automations: Automation[] | string[]): Promise<void> {
  const apiClient = createApiClient(workerId);

  for (const automation of automations) {
    const automationId = typeof automation === 'string' ? automation : automation.id;

    try {
      await apiClient.deleteAutomation(automationId);
      testLog.info(`Cleaned up automation ${automationId}`);
    } catch (error) {
      testLog.warn(`Failed to clean up automation ${automationId}:`, error);
    }
  }
}

/**
 * Get the automation count for a worker.
 */
export async function getAutomationCount(workerId: string): Promise<number> {
  const apiClient = createApiClient(workerId);

  try {
    const automations = await apiClient.listAutomations();
    return automations.length;
  } catch (error) {
    testLog.warn(`Failed to get automation count for worker ${workerId}:`, error);
    return 0;
  }
}

/**
 * Navigate to chat for a specific automation.
 */
export async function navigateToAutomationChat(page: Page, automationId: string): Promise<void> {
  await page.locator(`[data-testid="chat-automation-${automationId}"]`).click();

  // Wait for chat interface to load
  try {
    await page.waitForSelector('[data-testid="chat-input"]', { timeout: 5000 });
    testLog.info(`Navigated to chat for automation ${automationId}`);
  } catch (error) {
    testLog.warn(`Chat UI not fully loaded for automation ${automationId}, continuing...`);
  }
}

/**
 * Create a test automation using the API client via page context.
 * This is a convenience wrapper for tests that have a Page but need API-backed automations.
 */
export async function createTestAutomation(page: Page, name: string): Promise<Automation> {
  const workerId = process.env.TEST_PARALLEL_INDEX ?? process.env.TEST_WORKER_INDEX ?? '0';
  const apiClient = createApiClient(workerId);

  const config: CreateAutomationRequest = {
    name: name || `Test Automation ${Date.now()}`,
    model: 'deepseek/deepseek-v4-flash',
    system_instructions: 'You are a test automation for E2E testing',
    task_instructions: 'Perform test tasks as requested',
    temperature: 0.7,
  };

  const automation = await apiClient.createAutomation(config);
  testLog.info(`Test automation created: ${automation.name} (ID: ${automation.id})`);
  return automation;
}
