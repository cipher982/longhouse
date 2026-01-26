/**
 * Data Persistence Tests - Core Suite
 *
 * Tests that data survives navigation and page reload.
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
 * Create an fiche via UI and return its ID.
 * CRITICAL: Gets ID from API response, NOT from DOM query (.first() is racy in parallel tests)
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
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    const testMessage = 'Persistence test message';
    await sendMessage(page, testMessage);

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toContainText(testMessage, { timeout: 15000 });

    // Navigate away
    await page.goto('/');
    await expect(page.locator('[data-testid="create-fiche-btn"]')).toBeVisible({ timeout: 10000 });

    // Navigate back
    await navigateToChat(page, ficheId);

    // Message should still be there
    await expect(messagesContainer).toContainText(testMessage, { timeout: 15000 });
  });

  test('message persists after direct URL navigation', async ({ page }) => {
    const ficheId = await createFicheViaUI(page);
    await navigateToChat(page, ficheId);

    const persistentMessage = 'This should persist';
    await sendMessage(page, persistentMessage);

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toContainText(persistentMessage, { timeout: 15000 });

    // Capture thread URL
    const threadUrl = page.url();

    // Navigate to dashboard
    await page.goto('/');
    await expect(page.locator('[data-testid="create-fiche-btn"]')).toBeVisible({ timeout: 10000 });

    // Navigate back to the exact thread URL
    await page.goto(threadUrl);
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

    // Message should persist
    await expect(messagesContainer).toContainText(persistentMessage, { timeout: 15000 });
  });
});
