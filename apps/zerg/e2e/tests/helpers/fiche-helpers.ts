import { testLog } from './test-logger';

import { Page, expect } from '@playwright/test';
import { createApiClient, Fiche, CreateFicheRequest } from './api-client';

/**
 * Fiche lifecycle helpers for E2E tests
 * Provides consistent patterns for fiche creation, management, and cleanup
 */

export interface FicheCreationOptions {
  name?: string;
  model?: string;
  systemInstructions?: string;
  taskInstructions?: string;
  temperature?: number;
  retries?: number;
}

export interface FicheBatchOptions {
  count: number;
  namePrefix?: string;
  model?: string;
  systemInstructions?: string;
  taskInstructions?: string;
}

/**
 * Create a single fiche via API with sensible defaults
 */
export async function createFicheViaAPI(
  commisId: string,
  options: FicheCreationOptions = {}
): Promise<Fiche> {
  const apiClient = createApiClient(commisId);

  const config: CreateFicheRequest = {
    name: options.name || `Test Fiche ${commisId}`,
    model: options.model || 'gpt-5-nano',
    system_instructions: options.systemInstructions || 'You are a test fiche for E2E testing',
    task_instructions: options.taskInstructions || 'Perform test tasks as requested',
    temperature: options.temperature || 0.7,
  };

  const retries = options.retries || 3;
  let attempts = 0;

  while (attempts < retries) {
    try {
      const fiche = await apiClient.createFiche(config);
      testLog.info(`✅ Fiche created via API: ${fiche.name} (ID: ${fiche.id})`);
      return fiche;
    } catch (error) {
      attempts++;
      testLog.warn(`Fiche creation attempt ${attempts} failed:`, error);

      if (attempts >= retries) {
        throw new Error(`Failed to create fiche after ${retries} attempts: ${error}`);
      }

      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  }

  throw new Error('Unexpected error in fiche creation');
}

/**
 * Create multiple fiches in batch
 */
export async function createMultipleFiches(
  commisId: string,
  options: FicheBatchOptions
): Promise<Fiche[]> {
  const fiches: Fiche[] = [];
  const namePrefix = options.namePrefix || 'Batch Fiche';

  for (let i = 0; i < options.count; i++) {
    const fiche = await createFicheViaAPI(commisId, {
      name: `${namePrefix} ${i + 1}`,
      model: options.model,
      systemInstructions: options.systemInstructions,
      taskInstructions: options.taskInstructions,
    });
    fiches.push(fiche);
  }

  testLog.info(`✅ Created ${fiches.length} fiches in batch`);
  return fiches;
}

/**
 * Create fiche via UI and return its ID
 * CRITICAL: Gets ID from API response, NOT from DOM query (.first() is racy in parallel tests)
 */
export async function createFicheViaUI(page: Page): Promise<string> {
  // Navigate to dashboard if not already there
  try {
    await page.locator('.header-nav').click();
    await page.waitForTimeout(500);
  } catch {
    // Ignore if already on dashboard or button doesn't exist
  }

  const createBtn = page.locator('[data-testid="create-fiche-btn"]');
  await expect(createBtn).toBeVisible({ timeout: 10000 });
  await expect(createBtn).toBeEnabled({ timeout: 5000 });

  // Capture API response to get the ACTUAL created fiche ID
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/api/fiches') && r.request().method() === 'POST' && r.status() === 201,
      { timeout: 10000 }
    ),
    createBtn.click(),
  ]);

  // Parse the fiche ID from the response body - this is deterministic
  const body = await response.json();
  const ficheId = String(body.id);

  if (!ficheId || ficheId === 'undefined') {
    throw new Error(`Failed to get fiche ID from API response: ${JSON.stringify(body)}`);
  }

  // Wait for THIS SPECIFIC fiche's row to appear (not just any row)
  const newRow = page.locator(`tr[data-fiche-id="${ficheId}"]`);
  await expect(newRow).toBeVisible({ timeout: 10000 });

  testLog.info(`✅ Fiche created via UI with ID: ${ficheId}`);
  return ficheId;
}

/**
 * Get fiche by ID with retry logic
 */
export async function getFicheById(commisId: string, ficheId: string): Promise<Fiche | null> {
  const apiClient = createApiClient(commisId);

  try {
    const fiches = await apiClient.listFiches();
    return fiches.find(fiche => fiche.id === ficheId) || null;
  } catch (error) {
    testLog.warn(`Failed to get fiche ${ficheId}:`, error);
    return null;
  }
}

/**
 * Verify fiche exists and has expected properties
 */
export async function verifyFicheExists(
  commisId: string,
  ficheId: string,
  expectedName?: string
): Promise<boolean> {
  const fiche = await getFicheById(commisId, ficheId);

  if (!fiche) {
    testLog.warn(`Fiche ${ficheId} not found`);
    return false;
  }

  if (expectedName && fiche.name !== expectedName) {
    testLog.warn(`Fiche ${ficheId} has name "${fiche.name}", expected "${expectedName}"`);
    return false;
  }

  return true;
}

/**
 * Wait for fiche to appear in UI
 */
export async function waitForFicheInUI(page: Page, ficheId: string, timeout: number = 10000): Promise<void> {
  await expect(page.locator(`tr[data-fiche-id="${ficheId}"]`)).toBeVisible({ timeout });
}

/**
 * Edit fiche via UI modal
 */
export async function editFicheViaUI(
  page: Page,
  ficheId: string,
  data: {
    name?: string;
    systemInstructions?: string;
    taskInstructions?: string;
    temperature?: number;
    model?: string;
  }
): Promise<void> {
  // Open edit modal
  await page.locator(`[data-testid="edit-fiche-${ficheId}"]`).click();
  await expect(page.locator('#fiche-modal')).toBeVisible({ timeout: 5000 });
  await page.waitForSelector('#fiche-name:not([disabled])', { timeout: 5000 });

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

  // Wait for modal to close
  await expect(page.locator('#fiche-modal')).not.toBeVisible({ timeout: 5000 });

  testLog.info(`✅ Fiche ${ficheId} edited via UI`);
}

/**
 * Delete fiche via UI with confirmation
 */
export async function deleteFicheViaUI(page: Page, ficheId: string, confirm: boolean = true): Promise<void> {
  // Set up dialog handler
  page.once('dialog', (dialog) => {
    if (confirm) {
      dialog.accept();
    } else {
      dialog.dismiss();
    }
  });

  // Click delete button
  await page.locator(`[data-testid="delete-fiche-${ficheId}"]`).click();

  if (confirm) {
    // Wait for row to disappear
    await expect(page.locator(`tr[data-fiche-id="${ficheId}"]`)).toHaveCount(0, { timeout: 5000 });
    testLog.info(`✅ Fiche ${ficheId} deleted via UI`);
  } else {
    // Row should still be present
    await expect(page.locator(`tr[data-fiche-id="${ficheId}"]`)).toHaveCount(1);
    testLog.info(`✅ Fiche ${ficheId} deletion cancelled`);
  }
}

/**
 * Delete fiche via API
 */
export async function deleteFicheViaAPI(commisId: string, ficheId: string): Promise<void> {
  const apiClient = createApiClient(commisId);

  try {
    await apiClient.deleteFiche(ficheId);
    testLog.info(`✅ Fiche ${ficheId} deleted via API`);
  } catch (error) {
    testLog.warn(`Failed to delete fiche ${ficheId}:`, error);
    throw error;
  }
}

/**
 * Cleanup multiple fiches
 */
export async function cleanupFiches(commisId: string, fiches: Fiche[] | string[]): Promise<void> {
  const apiClient = createApiClient(commisId);

  for (const fiche of fiches) {
    const ficheId = typeof fiche === 'string' ? fiche : fiche.id;

    try {
      await apiClient.deleteFiche(ficheId);
      testLog.info(`✅ Cleaned up fiche ${ficheId}`);
    } catch (error) {
      testLog.warn(`Failed to cleanup fiche ${ficheId}:`, error);
    }
  }
}

/**
 * Get fiche count for a commis
 */
export async function getFicheCount(commisId: string): Promise<number> {
  const apiClient = createApiClient(commisId);

  try {
    const fiches = await apiClient.listFiches();
    return fiches.length;
  } catch (error) {
    testLog.warn(`Failed to get fiche count for commis ${commisId}:`, error);
    return 0;
  }
}

/**
 * Navigate to chat for a specific fiche
 */
export async function navigateToFicheChat(page: Page, ficheId: string): Promise<void> {
  await page.locator(`[data-testid="chat-fiche-${ficheId}"]`).click();

  // Wait for chat interface to load
  try {
    await page.waitForSelector('[data-testid="chat-input"]', { timeout: 5000 });
    testLog.info(`✅ Navigated to chat for fiche ${ficheId}`);
  } catch (error) {
    testLog.warn(`Chat UI not fully loaded for fiche ${ficheId}, continuing...`);
  }
}

/**
 * Create a test fiche using the API client via page context
 * This is a convenience wrapper for tests that have a Page but need to create fiches
 */
export async function createTestFiche(page: Page, name: string): Promise<Fiche> {
  const commisId = process.env.TEST_PARALLEL_INDEX ?? process.env.TEST_WORKER_INDEX ?? '0';
  const apiClient = createApiClient(commisId);

  const config: CreateFicheRequest = {
    name: name || `Test Fiche ${Date.now()}`,
    model: 'gpt-5-nano',
    system_instructions: 'You are a test fiche for E2E testing',
    task_instructions: 'Perform test tasks as requested',
    temperature: 0.7,
  };

  const fiche = await apiClient.createFiche(config);
  testLog.info(`✅ Test fiche created: ${fiche.name} (ID: ${fiche.id})`);
  return fiche;
}
