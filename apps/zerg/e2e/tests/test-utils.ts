/**
 * Shared test utilities for E2E tests
 *
 * These helpers are designed to be:
 * - DETERMINISTIC: Wait for specific conditions, not timeouts
 * - ISOLATED: Each operation is self-contained
 * - ROBUST: Handle race conditions properly
 */

import { expect, type Page, type APIRequestContext } from '@playwright/test';

/**
 * Create an fiche via UI and return its ID.
 *
 * CRITICAL: Gets ID from API response, NOT from DOM query.
 * This prevents race conditions where .first() returns a stale row.
 */
export async function createFicheViaUI(page: Page): Promise<string> {
  await page.goto('/');

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

  // Parse the fiche ID from the response body
  const body = await response.json();
  const ficheId = String(body.id);

  if (!ficheId || ficheId === 'undefined') {
    throw new Error(`Failed to get fiche ID from API response: ${JSON.stringify(body)}`);
  }

  // Wait for THIS SPECIFIC fiche's row to appear in DOM (not just any row)
  const row = page.locator(`tr[data-fiche-id="${ficheId}"]`);
  await expect(row).toBeVisible({ timeout: 10000 });

  return ficheId;
}

/**
 * Create an fiche via API (faster, for tests that don't need UI verification)
 */
export async function createFicheViaAPI(request: APIRequestContext): Promise<string> {
  const response = await request.post('/api/fiches', {
    data: { name: `Test Fiche ${Date.now()}` }
  });

  if (response.status() !== 201) {
    throw new Error(`Failed to create fiche: ${response.status()} ${await response.text()}`);
  }

  const body = await response.json();
  return String(body.id);
}

/**
 * Navigate to chat for an fiche.
 * Waits for URL change and chat UI to be fully ready.
 */
export async function navigateToChat(page: Page, ficheId: string): Promise<void> {
  const chatBtn = page.locator(`[data-testid="chat-fiche-${ficheId}"]`);
  await expect(chatBtn).toBeVisible({ timeout: 10000 });
  await chatBtn.click();

  // Wait for URL to change to the fiche's chat
  await page.waitForURL((url) => url.pathname.includes(`/fiche/${ficheId}/thread`), { timeout: 10000 });

  // Wait for chat UI to be fully interactive
  await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });
  await expect(page.locator('[data-testid="chat-input"]')).toBeEnabled({ timeout: 5000 });
}

/**
 * Navigate to dashboard and wait for it to be ready
 */
export async function navigateToDashboard(page: Page): Promise<void> {
  await page.goto('/');

  // Wait for dashboard to be fully loaded - the create button is a reliable signal
  const createBtn = page.locator('[data-testid="create-fiche-btn"]');
  await expect(createBtn).toBeVisible({ timeout: 10000 });
  await expect(createBtn).toBeEnabled({ timeout: 5000 });
}

/**
 * Send a message and wait for API response.
 * Does NOT wait for LLM response - only for message POST to succeed.
 */
export async function sendMessage(page: Page, message: string): Promise<void> {
  const input = page.locator('[data-testid="chat-input"]');
  await expect(input).toBeEnabled({ timeout: 5000 });
  await input.fill(message);

  const sendBtn = page.locator('[data-testid="send-message-btn"]');
  await expect(sendBtn).toBeEnabled({ timeout: 5000 });

  // Wait for message POST to complete
  await Promise.all([
    page.waitForResponse(
      (r) =>
        r.url().includes('/api/threads/') &&
        r.url().includes('/messages') &&
        r.request().method() === 'POST' &&
        (r.status() === 200 || r.status() === 201),
      { timeout: 15000 }
    ),
    sendBtn.click(),
  ]);
}

/**
 * Create a new thread and wait for API response.
 * Returns the thread ID.
 */
export async function createNewThread(page: Page): Promise<number> {
  const newThreadBtn = page.locator('[data-testid="new-thread-btn"]');
  await expect(newThreadBtn).toBeVisible({ timeout: 5000 });

  const [response] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        r.status() === 201 &&
        new URL(r.url()).pathname === '/api/threads',
      { timeout: 10000 }
    ),
    newThreadBtn.click(),
  ]);

  const body = await response.json();
  return body.id;
}

/**
 * Wait for a user message to appear in the chat
 */
export async function waitForUserMessage(page: Page, messageText: string): Promise<void> {
  const userMessage = page.locator('.message.user').filter({ hasText: messageText });
  await expect(userMessage).toBeVisible({ timeout: 10000 });
}

/**
 * Wait for an assistant message to appear in the chat
 */
export async function waitForAssistantMessage(page: Page): Promise<void> {
  const assistantMessage = page.locator('.message.assistant');
  await expect(assistantMessage).toBeVisible({ timeout: 30000 });
}

/**
 * Reset database to clean state (call in beforeEach).
 * STRICT: Throws on failure to fail fast and avoid dirty state propagation.
 * Includes aggressive retry logic to handle lock contention under high concurrency.
 * Adds initial stagger delay to prevent all commis from hitting reset simultaneously.
 */
export async function resetDatabase(request: APIRequestContext): Promise<void> {
  const maxRetries = 5;
  const baseDelay = 200;
  const maxJitter = 300; // Wider spread to reduce concurrent retries

  // Add initial stagger delay (0-500ms) to spread out reset calls across commis
  // This prevents all beforeEach hooks from hitting the backend simultaneously
  await new Promise(r => setTimeout(r, Math.random() * 500));

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      const response = await request.post('/admin/reset-database', {
        data: { reset_type: 'clear_data' },
        timeout: 15000, // Explicit 15s timeout (backend has 30s statement_timeout)
      });

      if (response.ok()) {
        return;
      }

      // On 500 errors, retry with exponential backoff + wide jitter
      if (response.status() === 500 && attempt < maxRetries) {
        const delay = baseDelay * Math.pow(2, attempt - 1) + Math.random() * maxJitter;
        await new Promise(r => setTimeout(r, delay));
        continue;
      }

      throw new Error(`Database reset failed: ${response.status()} after ${attempt} attempts - tests cannot continue with dirty state`);
    } catch (error) {
      // Handle network errors (timeouts, connection refused) with retry
      if (attempt < maxRetries) {
        const delay = baseDelay * Math.pow(2, attempt - 1) + Math.random() * maxJitter;
        await new Promise(r => setTimeout(r, delay));
        continue;
      }
      throw new Error(`Database reset failed after ${attempt} attempts: ${error}`);
    }
  }
}
