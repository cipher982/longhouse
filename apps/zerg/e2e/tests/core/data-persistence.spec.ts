/**
 * Data Persistence Tests - Core Suite
 *
 * Tests that data survives navigation and page reload.
 *
 * CORE SUITE: 0 skipped, 0 flaky, retries: 0
 */

import { test, expect, type Page } from '../fixtures';
import { waitForPageReady } from '../helpers/ready-signals';
import { resetDatabase } from '../test-utils';

// Reset DB before each test for clean state
// Uses strict reset that throws on failure to fail fast
test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

/**
 * Create an automation via UI and return its ID.
 * CRITICAL: Gets the ID from the API response, not from the DOM.
 */
async function createAutomationViaUI(page: Page): Promise<string> {
  await page.goto('/automations');
  await waitForPageReady(page, { timeout: 20000 });

  const createBtn = page.locator('[data-testid="create-automation-btn"]');
  await expect(createBtn).toBeVisible({ timeout: 20000 });
  await expect(createBtn).toBeEnabled({ timeout: 20000 });

  // Capture the API response to get the actual created automation ID.
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/api/automations') && r.request().method() === 'POST' && r.status() === 201,
      { timeout: 20000 }
    ),
    createBtn.click(),
  ]);

  // Parse the automation ID from the response body. This is deterministic.
  const body = await response.json();
  const automationId = String(body.id);

  if (!automationId || automationId === 'undefined') {
    throw new Error(`Failed to get automation ID from API response: ${JSON.stringify(body)}`);
  }

  const row = page.locator(`tr[data-automation-id="${automationId}"]`);
  await expect(row).toBeVisible({ timeout: 20000 });

  return automationId;
}

/**
 * Navigate to chat for an automation.
 */
async function navigateToChat(page: Page, automationId: string): Promise<void> {
  const chatBtn = page.locator(`[data-testid="chat-automation-${automationId}"]`);
  await expect(chatBtn).toBeVisible({ timeout: 10000 });
  await chatBtn.click();

  await page.waitForURL((url) => url.pathname.includes(`/automations/${automationId}/thread`), { timeout: 20000 });
  await expect(page.locator('[data-testid="chat-page"]')).toBeVisible({ timeout: 20000 });
  await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 20000 });
  await expect(page.locator('[data-testid="chat-input"]')).toBeEnabled({ timeout: 20000 });
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
    const automationId = await createAutomationViaUI(page);
    await navigateToChat(page, automationId);

    const testMessage = 'Persistence test message';
    await sendMessage(page, testMessage);

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toContainText(testMessage, { timeout: 15000 });

    // Navigate away
    await page.goto('/automations');
    await waitForPageReady(page, { timeout: 20000 });
    await expect(page.locator('[data-testid="create-automation-btn"]')).toBeVisible({ timeout: 20000 });

    // Navigate back
    await navigateToChat(page, automationId);

    // Message should still be there
    await expect(messagesContainer).toContainText(testMessage, { timeout: 15000 });
  });

  test('message persists after direct URL navigation', async ({ page }) => {
    const automationId = await createAutomationViaUI(page);
    await navigateToChat(page, automationId);

    const persistentMessage = 'This should persist';
    await sendMessage(page, persistentMessage);

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toContainText(persistentMessage, { timeout: 15000 });

    // Capture thread URL
    const threadUrl = page.url();

    // Navigate to automations
    await page.goto('/automations');
    await waitForPageReady(page, { timeout: 20000 });
    await expect(page.locator('[data-testid="create-automation-btn"]')).toBeVisible({ timeout: 20000 });

    // Navigate back to the exact thread URL
    await page.goto(threadUrl);
    // Reloading the chat view can take longer while data refetches on remote DBs.
    await expect(page.locator('[data-testid="chat-page"]')).toBeVisible({ timeout: 20000 });
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 20000 });

    // Message should persist
    await expect(messagesContainer).toContainText(persistentMessage, { timeout: 15000 });
  });
});
