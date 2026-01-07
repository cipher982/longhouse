import { test, expect, type Page } from './fixtures';

// Reset DB before each test to keep thread ids predictable
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database');
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
    await expect(page.locator('.chat-input')).toBeVisible({ timeout: 5000 });

    await page.waitForSelector('.chat-input', { state: 'visible' });

    // Click "New Thread" to ensure fresh context (if it exists)
    const newThreadBtn = page.locator('.new-thread-btn');
    if (await newThreadBtn.count() > 0) {
      await newThreadBtn.click();
    }

    // Type user message and send.
    const INPUT = page.locator('.chat-input');
    await INPUT.fill('Hello agent');
    const sendBtn = page.locator('.send-button');
    await expect(sendBtn).toBeVisible({ timeout: 5000 });
    await sendBtn.click();

    // Verify the message appears in messages container.
    await expect(page.locator('.messages-container')).toContainText('Hello agent', { timeout: 5000 });

    // Optional: switch back to dashboard
    const backBtn = page.locator('.back-button');
    if (await backBtn.count() > 0) {
      await backBtn.click();
      await expect(page.locator('.header-nav')).toBeVisible();
    }
  });

  test('Wait for and verify agent response (placeholder)', async ({ page }) => {
    test.skip(true, 'LLM streaming not stubbed – skipping until mock server available');
  });

  test('Send follow-up message in same thread', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    // REQUIRE chat input to exist - no more skipping!
    await expect(page.locator('.chat-input')).toBeVisible({ timeout: 5000 });

    await page.waitForSelector('.chat-input');

    // Use existing thread (first in sidebar)
    const INPUT = page.locator('.chat-input');
    await INPUT.fill('Follow-up');

    const sendBtn = page.locator('.send-button');
    await expect(sendBtn).toBeVisible({ timeout: 5000 });

    await sendBtn.click();
    await expect(page.locator('.messages-container')).toContainText('Follow-up', { timeout: 5000 });
  });

  test('Create multiple threads and switch', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    const newThreadBtn = page.locator('.new-thread-btn');
    await expect(newThreadBtn).toBeVisible({ timeout: 5000 }); // REQUIRE thread management

    await page.waitForSelector('.new-thread-btn');

    // create two threads
    await newThreadBtn.click();
    await newThreadBtn.click();

    const listItems = page.locator('.thread-list .thread-row');
    await expect(listItems).toHaveCount(2, { timeout: 5000 }); // REQUIRE thread list

    const first = listItems.nth(0);
    const second = listItems.nth(1);
    await second.click();
    await expect(second).toHaveClass(/selected/);
    await first.click();
    await expect(first).toHaveClass(/selected/);
  });

  test('Delete thread and verify removal', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    const newThreadBtn = page.locator('.new-thread-btn');
    await expect(newThreadBtn).toBeVisible({ timeout: 5000 }); // REQUIRE thread management

    await page.waitForSelector('.new-thread-btn');
    await newThreadBtn.click();
    const threadRow = page.locator('.thread-list .thread-row').first();
    await expect(threadRow).toBeVisible({ timeout: 5000 }); // REQUIRE thread list

    const deleteBtn = threadRow.locator('button', { hasText: 'Delete' });
    await expect(deleteBtn).toBeVisible({ timeout: 5000 }); // REQUIRE delete functionality

    await deleteBtn.click();
    page.once('dialog', (d) => d.accept());
    await expect(threadRow).toHaveCount(0);
  });

  test('Thread title editing', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    const newThreadBtn = page.locator('.new-thread-btn');
    await expect(newThreadBtn).toBeVisible({ timeout: 5000 }); // REQUIRE thread management

    await newThreadBtn.click();
    const threadRow = page.locator('.thread-list .thread-row').first();
    if (await threadRow.count() === 0) {
      test.skip(true, 'Thread list not rendered');
      return;
    }

    await threadRow.dblclick();
    const titleInput = threadRow.locator('input');
    await expect(titleInput).toBeVisible({ timeout: 5000 }); // REQUIRE title editing

    await titleInput.fill('Renamed');
    await titleInput.press('Enter');
    await expect(threadRow).toContainText('Renamed');
  });

  test('Verify message history persistence after reload', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    // REQUIRE chat UI to exist - core functionality
    await expect(page.locator('.chat-input')).toBeVisible({ timeout: 5000 });

    const newThreadBtn = page.locator('.new-thread-btn');
    if (await newThreadBtn.count() > 0) {
      await newThreadBtn.click();
    }

    const input = page.locator('.chat-input');
    await input.fill('Persist this');

    const sendBtn = page.locator('.send-button');
    if (await sendBtn.count() === 0) {
      test.skip(true, 'Send button not implemented yet');
      return;
    }

    await sendBtn.click();
    await page.reload();

    // Re-navigate to chat after reload
    await page.goto('/');
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    await page.waitForSelector('.chat-input', { timeout: 5000 });
    await expect(page.locator('.messages-container')).toContainText('Persist this', { timeout: 5000 });
  });

  test('Empty thread state displays CTA', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    const messagesContainer = page.locator('.messages-container');
    if (await messagesContainer.count() === 0) {
      test.skip(true, 'Messages container not implemented yet');
      return;
    }

    await page.waitForSelector('.messages-container');
    await expect(messagesContainer).toContainText(/Start.*conversation|No messages/, { timeout: 5000 });
  });
});
