/**
 * Data Persistence Tests - Core Suite
 *
 * Tests that data survives navigation and page reload.
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

test.describe('Data Persistence - Core', () => {
  test('message persists after navigation', async ({ page }) => {
    const agentId = await createAgentViaUI(page);
    await navigateToChat(page, agentId);

    const testMessage = 'Persistence test message';
    await sendMessage(page, testMessage);

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toContainText(testMessage, { timeout: 15000 });

    // Navigate away
    await page.goto('/');
    await expect(page.locator('[data-testid="create-agent-btn"]')).toBeVisible({ timeout: 10000 });

    // Navigate back
    await navigateToChat(page, agentId);

    // Message should still be there
    await expect(messagesContainer).toContainText(testMessage, { timeout: 15000 });
  });

  test('message persists after direct URL navigation', async ({ page }) => {
    const agentId = await createAgentViaUI(page);
    await navigateToChat(page, agentId);

    const persistentMessage = 'This should persist';
    await sendMessage(page, persistentMessage);

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toContainText(persistentMessage, { timeout: 15000 });

    // Capture thread URL
    const threadUrl = page.url();

    // Navigate to dashboard
    await page.goto('/');
    await expect(page.locator('[data-testid="create-agent-btn"]')).toBeVisible({ timeout: 10000 });

    // Navigate back to the exact thread URL
    await page.goto(threadUrl);
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

    // Message should persist
    await expect(messagesContainer).toContainText(persistentMessage, { timeout: 15000 });
  });
});
