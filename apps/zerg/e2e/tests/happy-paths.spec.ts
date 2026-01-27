/**
 * HAPPY PATH TESTS - Core User Journeys
 *
 * This is the CANONICAL test file for essential user flows.
 * As the only QA for this solo project, these tests must cover
 * everything a real user would do.
 *
 * Strategy:
 * - Each test validates ONE invariant
 * - All waits are deterministic (API responses, element states)
 * - No arbitrary timeouts or networkidle waits
 * - Tests are isolated (reset DB per test)
 *
 * Coverage:
 * - AGENT: Create, verify in dashboard
 * - CHAT: Navigate, send message, verify display
 * - THREAD: Create, switch, rename, verify isolation
 * - NAVIGATION: Browser back/forward, state persistence
 * - URL CONTRACT: Validate URL structure and behavior
 */

import { test, expect, type Page } from './fixtures';
import { resetDatabase } from './test-utils';

// Reset DB before each test for clean, isolated state
// Uses strict reset that throws on failure to fail fast
test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

// ============================================================================
// HELPERS - Reusable, deterministic operations
// ============================================================================

/**
 * Create an fiche via UI and return its ID.
 * CRITICAL: Gets ID from API response, NOT from DOM query (.first() is racy).
 */
async function createFicheViaUI(page: Page): Promise<string> {
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

  // Parse the fiche ID from the response body - this is deterministic
  const body = await response.json();
  const ficheId = String(body.id);

  if (!ficheId || ficheId === 'undefined') {
    throw new Error(`Failed to get fiche ID from API response: ${JSON.stringify(body)}`);
  }

  // Wait for THIS SPECIFIC fiche's row to appear (not just any row)
  const row = page.locator(`tr[data-fiche-id="${ficheId}"]`);
  await expect(row).toBeVisible({ timeout: 10000 });

  return ficheId;
}

/**
 * Navigate to chat for an fiche.
 * Waits for URL change and chat input to be ready.
 */
async function navigateToChat(page: Page, ficheId: string): Promise<void> {
  const chatBtn = page.locator(`[data-testid="chat-fiche-${ficheId}"]`);
  await expect(chatBtn).toBeVisible({ timeout: 5000 });
  await chatBtn.click();

  await page.waitForURL((url) => url.pathname.includes(`/fiche/${ficheId}/thread`), { timeout: 10000 });
  await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });
  await expect(page.locator('[data-testid="chat-input"]')).toBeEnabled({ timeout: 5000 });
}

/**
 * Send a message and wait for API response.
 * Does NOT wait for LLM response - only for message POST to succeed.
 */
async function sendMessage(page: Page, message: string): Promise<void> {
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
 */
async function createNewThread(page: Page): Promise<number> {
  const newThreadBtn = page.locator('[data-testid="new-thread-btn"]');
  await expect(newThreadBtn).toBeVisible({ timeout: 5000 });

  const [createResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        r.status() === 201 &&
        (() => {
          try {
            return new URL(r.url()).pathname === '/api/threads';
          } catch {
            return r.url().includes('/api/threads');
          }
        })(),
      { timeout: 15000 }
    ),
    newThreadBtn.click(),
  ]);

  const createdThread = await createResponse.json();
  const newThreadId = createdThread?.id;
  if (typeof newThreadId !== 'number') {
    throw new Error(`Expected create thread response to include numeric id, got: ${JSON.stringify(createdThread)}`);
  }

  // Wait for URL to include new thread id and UI selection to update
  await page.waitForURL((url) => url.pathname.includes(`/thread/${newThreadId}`), { timeout: 15000 });
  const threadRow = page.locator(`[data-testid="thread-row-${newThreadId}"]`);
  await expect(threadRow).toBeVisible({ timeout: 15000 });
  await expect(threadRow).toHaveClass(/selected/, { timeout: 15000 });

  // Wait for chat input to be ready after thread creation
  await expect(page.locator('[data-testid="chat-input"]')).toBeEnabled({ timeout: 5000 });

  return newThreadId;
}

// ============================================================================
// SMOKE TESTS - Core functionality that must always work
// ============================================================================

test.describe('Smoke Tests - Core Functionality', () => {
  test('SMOKE 1: Create Fiche - fiche appears in dashboard', async ({ page }) => {
    await page.goto('/');

    const createBtn = page.locator('[data-testid="create-fiche-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 10000 });
    await expect(createBtn).toBeEnabled({ timeout: 5000 });

    // Capture API response to get the ACTUAL created fiche ID (deterministic, no race)
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

    expect(ficheId).toBeTruthy();
    expect(ficheId).toMatch(/^\d+$/);

    // Wait for THIS SPECIFIC fiche's row to appear (not just any row via .first())
    const newRow = page.locator(`tr[data-fiche-id="${ficheId}"]`);
    await expect(newRow).toBeVisible({ timeout: 10000 });
  });

  test('SMOKE 2: Navigate to Chat - URL and UI are correct', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);

    await page.locator(`[data-testid="chat-fiche-${ficheId}"]`).click();
    await page.waitForURL((url) => url.pathname.includes(`/fiche/${ficheId}/thread`), { timeout: 10000 });
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

    const url = page.url();

    // URL must be either:
    // - /fiche/{id}/thread/ (with trailing slash, no thread ID)
    // - /fiche/{id}/thread/{tid} (with thread ID)
    const hasTrailingSlash = /\/thread\/(\?.*)?$/.test(url);
    const hasThreadId = /\/thread\/[a-zA-Z0-9-]+/.test(url);

    expect(hasTrailingSlash || hasThreadId, `URL must have trailing slash OR thread ID: ${url}`).toBeTruthy();
  });

  test('SMOKE 3: Send Message - message appears in chat', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    const testMessage = 'Hello, this is a smoke test message';
    await sendMessage(page, testMessage);

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toContainText(testMessage, { timeout: 15000 });
  });

  test('SMOKE 4: Input clears after sending message', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    const testMessage = 'Message to test input clearing';
    await sendMessage(page, testMessage);

    // Input should be cleared after send
    await expect(page.locator('[data-testid="chat-input"]')).toHaveValue('');
  });
});

// ============================================================================
// THREAD TESTS - Thread management contract
// ============================================================================

test.describe('Thread Management', () => {
  test('THREAD 1: Create new thread - URL changes and thread appears', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    const urlBeforeNewThread = page.url();
    const threadIdBeforeNewThread = urlBeforeNewThread.match(/\/thread\/([^/?]+)/)?.[1];

    const newThreadId = await createNewThread(page);
    if (threadIdBeforeNewThread) {
      expect(String(newThreadId)).not.toBe(threadIdBeforeNewThread);
    }

    // Thread list should have at least 2 threads
    const threadList = page.locator('.thread-list [data-testid^="thread-row-"]');
    await expect.poll(async () => await threadList.count(), { timeout: 10000 }).toBeGreaterThanOrEqual(2);
  });

  test('THREAD 2: Switch threads - selected class changes', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    // Create a second thread so we have two to switch between
    await createNewThread(page);

    const threadList = page.locator('.thread-list [data-testid^="thread-row-"]');
    await expect.poll(async () => await threadList.count(), { timeout: 10000 }).toBeGreaterThanOrEqual(2);

    const firstThread = threadList.nth(0);
    const secondThread = threadList.nth(1);

    // Click second thread and wait for selection
    await secondThread.click();
    await expect(secondThread).toHaveClass(/selected/, { timeout: 5000 });

    // Click first thread and wait for selection to switch
    await firstThread.click();
    await expect(firstThread).toHaveClass(/selected/, { timeout: 5000 });
  });

  test('THREAD 3: New thread starts empty - no message bleed', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    // Send message in first thread
    const thread1Message = 'UNIQUE_MESSAGE_THREAD_ONE_12345';
    await sendMessage(page, thread1Message);

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toContainText(thread1Message, { timeout: 15000 });

    // Create new thread
    await createNewThread(page);

    // New thread should NOT contain the first thread's message
    await expect(messagesContainer).toBeVisible({ timeout: 5000 });

    // Use polling to verify message is NOT present
    await expect
      .poll(async () => {
        const text = await messagesContainer.textContent();
        return text?.includes(thread1Message) ?? false;
      }, { timeout: 5000 })
      .toBe(false);
  });

  test('THREAD 4: Thread title editing', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    // Create a new thread
    await createNewThread(page);

    const threadRow = page.locator('.thread-list [data-testid^="thread-row-"]').first();
    await expect(threadRow).toBeVisible({ timeout: 5000 });

    const editBtn = threadRow.locator('[data-testid^="edit-thread-"]').first();
    await expect(editBtn).toBeVisible({ timeout: 5000 });
    await editBtn.click();

    const titleInput = threadRow.locator('input.thread-title-input');
    await expect(titleInput).toBeVisible({ timeout: 5000 });
    await expect(titleInput).toBeFocused({ timeout: 2000 });

    await titleInput.fill('Renamed Thread');

    // Wait for PUT response after pressing Enter
    await Promise.all([
      page.waitForResponse(
        (resp) => resp.url().includes('/api/threads/') && resp.request().method() === 'PUT',
        { timeout: 10000 }
      ),
      titleInput.press('Enter'),
    ]);

    await expect(threadRow).toContainText('Renamed', { timeout: 5000 });
  });
});

// ============================================================================
// PERSISTENCE TESTS - Data survives navigation/reload
// ============================================================================

test.describe('Data Persistence', () => {
  test('PERSIST 1: Message persists after navigation', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    const testMessage = 'Persistence test message';
    await sendMessage(page, testMessage);

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toContainText(testMessage, { timeout: 15000 });

    // Navigate away and back
    await page.goto('/');
    await expect(page.locator('[data-testid="create-fiche-btn"]')).toBeVisible({ timeout: 10000 });

    await navigateToChat(page, ficheId);

    // Message should still be there
    await expect(messagesContainer).toContainText(testMessage, { timeout: 15000 });
  });

  test('PERSIST 2: Message persists after page reload', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    const persistentMessage = 'This should persist after reload';
    await sendMessage(page, persistentMessage);

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toContainText(persistentMessage, { timeout: 15000 });

    // Capture thread URL
    const threadUrl = page.url();

    // Navigate to dashboard then back (reload redirects to dashboard in this app)
    await page.goto('/');
    await expect(page.locator('[data-testid="create-fiche-btn"]')).toBeVisible({ timeout: 10000 });

    // Navigate back to the exact thread URL
    await page.goto(threadUrl);
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

    // Message should persist
    await expect(messagesContainer).toContainText(persistentMessage, { timeout: 15000 });
  });
});

// ============================================================================
// URL CONTRACT TESTS - URL structure validation
// ============================================================================

test.describe('URL Contract', () => {
  test('URL 1: No trailing slash bug - thread path always valid', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);

    await page.locator(`[data-testid="chat-fiche-${ficheId}"]`).click();
    await page.waitForURL((url) => url.pathname.includes(`/fiche/${ficheId}/thread`), { timeout: 10000 });
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

    const url = page.url();

    // CRITICAL: URL ending in /thread (no slash) is a bug
    expect(url.match(/\/thread$/), `BUG: URL missing trailing slash: ${url}`).toBeFalsy();
  });

  test('URL 2: Thread ID preserved after sending message', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    const urlBeforeSend = page.url();
    const threadIdBefore = urlBeforeSend.match(/\/thread\/([^/?]+)/)?.[1];

    await sendMessage(page, 'Test message');

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toContainText('Test message', { timeout: 15000 });

    const urlAfterSend = page.url();
    const threadIdAfter = urlAfterSend.match(/\/thread\/([^/?]+)/)?.[1];

    // Thread ID should not change (no duplicate thread created)
    if (threadIdBefore && threadIdAfter) {
      expect(threadIdAfter).toBe(threadIdBefore);
    }
  });
});

// ============================================================================
// NAVIGATION TESTS - Browser navigation behavior
// ============================================================================

test.describe('Navigation', () => {
  test('NAV 1: Back to dashboard shows fiche list', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    // Go back to dashboard
    await page.goBack();
    await expect(page.locator('[data-testid="create-fiche-btn"]')).toBeVisible({ timeout: 10000 });

    // Fiche should still be visible
    await expect(page.locator(`tr[data-fiche-id="${ficheId}"]`)).toBeVisible({ timeout: 5000 });
  });
});

// ============================================================================
// CHAT UI TESTS - Chat interface behavior
// ============================================================================

test.describe('Chat UI', () => {
  test.skip('CHAT 1: Follow-up message in same thread', async ({ page }) => {
    // Skipped: This test requires waiting for LLM to finish processing the first message
    // before the send button is re-enabled for the follow-up. Without LLM mocking,
    // this test times out waiting for the LLM response.
    // Enable when mock LLM server is available.
    test.skip(true, 'LLM streaming not stubbed – skipping until mock server available');
  });

  test('CHAT 2: Empty thread displays appropriate state', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    // Wait for chat UI to be ready
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toBeVisible({ timeout: 5000 });

    // Check for empty state message
    await expect(messagesContainer).toContainText('No messages yet', { timeout: 5000 });
  });
});
