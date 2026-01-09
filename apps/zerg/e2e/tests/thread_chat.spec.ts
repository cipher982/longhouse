import { test, expect, type Page } from './fixtures';

// Reset DB before each test to keep thread ids predictable
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
});

async function createAgentAndGetId(page: Page): Promise<string> {
  await page.goto('/');
  await page.locator('[data-testid="create-agent-btn"]').click();
  const row = page.locator('tr[data-agent-id]').first();
  await expect(row).toBeVisible();
  return (await row.getAttribute('data-agent-id')) as string;
}

test.describe('Thread & Chat – basic flows', () => {
  test('Create new thread and send message', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);

    // Enter chat view via dashboard action button.
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    // Chat view should appear - REQUIRE that elements exist (no more skipping!)
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 5000 });

    // Click "New Thread" to ensure fresh context (if it exists)
    const newThreadBtn = page.locator('[data-testid="new-thread-btn"]');
    if (await newThreadBtn.count() > 0) {
      await newThreadBtn.click();
    }

    // Type user message and send.
    const input = page.locator('[data-testid="chat-input"]');
    await input.fill('Hello agent');
    const sendBtn = page.locator('[data-testid="send-message-btn"]');
    await expect(sendBtn).toBeVisible({ timeout: 5000 });
    await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          (r.status() === 200 || r.status() === 201) &&
          r.url().includes('/api/threads/') &&
          r.url().includes('/messages'),
        { timeout: 15000 }
      ),
      sendBtn.click(),
    ]);

    // Verify the message appears in messages container.
    await expect(page.locator('[data-testid="messages-container"]')).toContainText('Hello agent', { timeout: 15000 });
  });

  test('Wait for and verify agent response (placeholder)', async ({ page }) => {
    test.skip(true, 'LLM streaming not stubbed – skipping until mock server available');
  });

  test('Send follow-up message in same thread', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    // REQUIRE chat input to exist - no more skipping!
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 5000 });

    await page.waitForSelector('[data-testid="chat-input"]');

    // Use existing thread (first in sidebar)
    const input = page.locator('[data-testid="chat-input"]');
    await input.fill('Follow-up');

    const sendBtn = page.locator('[data-testid="send-message-btn"]');
    await expect(sendBtn).toBeVisible({ timeout: 5000 });

    await sendBtn.click();
    await expect(page.locator('[data-testid="messages-container"]')).toContainText('Follow-up', { timeout: 5000 });
  });

  test('Create multiple threads and switch', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    const newThreadBtn = page.locator('[data-testid="new-thread-btn"]');
    await expect(newThreadBtn).toBeVisible({ timeout: 5000 }); // REQUIRE thread management

    await page.waitForSelector('[data-testid="new-thread-btn"]');

    // create two threads
    await newThreadBtn.click();
    await newThreadBtn.click();

    const listItems = page.locator('.thread-list [data-testid^="thread-row-"]');
    await expect
      .poll(async () => await listItems.count(), { timeout: 5000 })
      .toBeGreaterThan(1); // REQUIRE thread list

    const first = listItems.nth(0);
    const second = listItems.nth(1);
    await second.click();
    await expect(second).toHaveClass(/selected/);
    await first.click();
    await expect(first).toHaveClass(/selected/);
  });

  test('Delete thread and verify removal', async ({ page }) => {
    test.skip(true, 'Thread deletion is not implemented in the UI yet');
  });

  test('Thread title editing', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    const newThreadBtn = page.locator('[data-testid="new-thread-btn"]');
    await expect(newThreadBtn).toBeVisible({ timeout: 5000 }); // REQUIRE thread management

    await newThreadBtn.click();
    const threadRow = page.locator('.thread-list [data-testid^="thread-row-"]').first();
    await expect(threadRow).toBeVisible({ timeout: 5000 }); // REQUIRE thread list

    const editBtn = threadRow.locator('[data-testid^="edit-thread-"]').first();
    await expect(editBtn).toBeVisible({ timeout: 5000 });

    await editBtn.click();
    const titleInput = threadRow.locator('input.thread-title-input');
    await expect(titleInput).toBeVisible({ timeout: 5000 }); // REQUIRE title editing

    await titleInput.fill('Renamed');
    await titleInput.press('Enter');
    await expect(threadRow).toContainText('Renamed');
  });

  test('Verify message history persistence after reload', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    // REQUIRE chat UI to exist - core functionality
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 5000 });

    const newThreadBtn = page.locator('[data-testid="new-thread-btn"]');
    if (await newThreadBtn.count() > 0) {
      await newThreadBtn.click();
    }

    const input = page.locator('[data-testid="chat-input"]');
    await input.fill('Persist this');

    const sendBtn = page.locator('[data-testid="send-message-btn"]');
    await expect(sendBtn).toBeVisible({ timeout: 5000 });

    // Wait for the API response to complete (not just optimistic UI update)
    const [response] = await Promise.all([
      page.waitForResponse(r => r.url().includes('/api/') && r.request().method() === 'POST' && r.status() === 200, { timeout: 15000 }),
      sendBtn.click(),
    ]);

    // Also verify UI shows the message
    await expect(page.locator('[data-testid="messages-container"]')).toContainText('Persist this', { timeout: 10000 });
    await page.reload();

    // Re-navigate to chat after reload
    await page.goto('/');
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    await page.waitForSelector('[data-testid="chat-input"]', { timeout: 5000 });
    await expect(page.locator('[data-testid="messages-container"]')).toContainText('Persist this', { timeout: 5000 });
  });

  test('Empty thread state displays CTA', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    const messagesContainer = page.locator('[data-testid="messages-container"]');
    await expect(messagesContainer).toBeVisible({ timeout: 5000 });
    await expect(messagesContainer).toContainText('No messages yet', { timeout: 5000 });
  });
});
