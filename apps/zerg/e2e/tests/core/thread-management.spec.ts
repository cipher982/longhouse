/**
 * Thread Management Tests - Core Suite
 *
 * Tests thread creation, switching, and isolation.
 *
 * CORE SUITE: 0 skipped, 0 flaky, retries: 0
 */

import { test, expect, type Page } from '../fixtures';

// Reset DB before each test for clean state
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
});

/**
 * Create an agent via UI and return its ID.
 */
async function createAgentViaUI(page: Page): Promise<string> {
  await page.goto('/');

  const createBtn = page.locator('[data-testid="create-agent-btn"]');
  await expect(createBtn).toBeVisible({ timeout: 10000 });
  await expect(createBtn).toBeEnabled({ timeout: 5000 });

  await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/api/agents') && r.request().method() === 'POST' && r.status() === 201,
      { timeout: 10000 }
    ),
    createBtn.click(),
  ]);

  const row = page.locator('tr[data-agent-id]').first();
  await expect(row).toBeVisible({ timeout: 10000 });

  const agentId = await row.getAttribute('data-agent-id');
  if (!agentId) {
    throw new Error('Failed to get agent ID from newly created agent row');
  }

  return agentId;
}

/**
 * Navigate to chat for an agent.
 */
async function navigateToChat(page: Page, agentId: string): Promise<void> {
  const chatBtn = page.locator(`[data-testid="chat-agent-${agentId}"]`);
  await expect(chatBtn).toBeVisible({ timeout: 5000 });
  await chatBtn.click();

  await page.waitForURL((url) => url.pathname.includes(`/agent/${agentId}/thread`), { timeout: 10000 });
  await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });
  await expect(page.locator('[data-testid="chat-input"]')).toBeEnabled({ timeout: 5000 });
}

/**
 * Send a message and wait for API response.
 */
async function sendMessage(page: Page, message: string): Promise<void> {
  const input = page.locator('[data-testid="chat-input"]');
  await expect(input).toBeEnabled({ timeout: 5000 });
  await input.fill(message);

  const sendBtn = page.locator('[data-testid="send-message-btn"]');
  await expect(sendBtn).toBeEnabled({ timeout: 5000 });

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

test.describe('Thread Management - Core', () => {
  test('create new thread - URL changes and thread appears', async ({ page }) => {
    const agentId = await createAgentViaUI(page);
    await navigateToChat(page, agentId);

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

  test('new thread starts empty - no message bleed', async ({ page }) => {
    const agentId = await createAgentViaUI(page);
    await navigateToChat(page, agentId);

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
      .poll(
        async () => {
          const text = await messagesContainer.textContent();
          return text?.includes(thread1Message) ?? false;
        },
        { timeout: 5000 }
      )
      .toBe(false);
  });
});
