/**
 * Chat Send Tests - Core Suite
 *
 * Tests basic chat functionality: send message, verify display.
 *
 * CORE SUITE: 0 skipped, 0 flaky, retries: 0
 */

import { test, expect, type Page } from '../fixtures';
import { resetDatabase } from '../test-utils';

// Reset DB before each test for clean state
// Uses strict reset that throws on failure to fail fast
test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

/**
 * Create an agent via UI and return its ID.
 * CRITICAL: Gets ID from API response, NOT from DOM query (.first() is racy in parallel tests)
 */
async function createAgentViaUI(page: Page): Promise<string> {
  await page.goto('/');

  const createBtn = page.locator('[data-testid="create-agent-btn"]');
  await expect(createBtn).toBeVisible({ timeout: 10000 });
  await expect(createBtn).toBeEnabled({ timeout: 5000 });

  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/api/agents') && r.request().method() === 'POST' && r.status() === 201,
      { timeout: 10000 }
    ),
    createBtn.click(),
  ]);

  const body = await response.json();
  const agentId = String(body.id);

  if (!agentId || agentId === 'undefined') {
    throw new Error(`Failed to get agent ID from API response: ${JSON.stringify(body)}`);
  }

  const row = page.locator(`tr[data-agent-id="${agentId}"]`);
  await expect(row).toBeVisible({ timeout: 10000 });

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

test.describe('Chat Send - Core', () => {
  test('send message - message appears in chat', async ({ page }) => {
    const agentId = await createAgentViaUI(page);
    await navigateToChat(page, agentId);

    const testMessage = 'Hello, this is a core test message';
    await sendMessage(page, testMessage);

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toContainText(testMessage, { timeout: 15000 });
  });

  test('input clears after sending message', async ({ page }) => {
    const agentId = await createAgentViaUI(page);
    await navigateToChat(page, agentId);

    const testMessage = 'Message to test input clearing';
    await sendMessage(page, testMessage);

    // Input should be cleared after send
    await expect(page.locator('[data-testid="chat-input"]')).toHaveValue('');
  });

  test('navigate to chat - URL is valid', async ({ page }) => {
    const agentId = await createAgentViaUI(page);

    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForURL((url) => url.pathname.includes(`/agent/${agentId}/thread`), { timeout: 10000 });
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

    const url = page.url();

    // URL must have trailing slash or thread ID (not bare /thread)
    const hasTrailingSlash = /\/thread\/(\?.*)?$/.test(url);
    const hasThreadId = /\/thread\/[a-zA-Z0-9-]+/.test(url);

    expect(hasTrailingSlash || hasThreadId, `URL must have trailing slash OR thread ID: ${url}`).toBeTruthy();
  });
});
