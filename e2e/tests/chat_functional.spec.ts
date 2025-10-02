import { test, expect, type Page } from './fixtures';

// Reset DB before each test to keep agent/thread ids predictable
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

test.describe('Chat Functional Tests - Complete Message Flow', () => {
  test('Send message and verify it appears in chat UI', async ({ page }) => {
    // Create agent and navigate to chat
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    // Verify chat UI loads
    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });
    await expect(page.getByTestId('send-message-btn')).toBeVisible({ timeout: 5000 });

    // Send a test message
    const testMessage = 'Hello, this is a functional test message';
    await page.getByTestId('chat-input').fill(testMessage);
    await page.getByTestId('send-message-btn').click();

    // **CRITICAL: Verify message actually appears in the UI**
    await expect(page.getByTestId('messages-container')).toContainText(testMessage, { 
      timeout: 10000 
    });

    // Verify message appears with correct metadata (user message)
    await expect(page.locator('[data-testid="chat-message"]')).toContainText(testMessage);
  });

  test('Send multiple messages and verify conversation state', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });

    // Send first message
    const message1 = 'First message';
    await page.getByTestId('chat-input').fill(message1);
    await page.getByTestId('send-message-btn').click();
    
    // Verify first message appears
    await expect(page.getByTestId('messages-container')).toContainText(message1, { timeout: 10000 });

    // Send second message
    const message2 = 'Second message';
    await page.getByTestId('chat-input').fill(message2);
    await page.getByTestId('send-message-btn').click();

    // Verify both messages appear in conversation
    await expect(page.getByTestId('messages-container')).toContainText(message1);
    await expect(page.getByTestId('messages-container')).toContainText(message2);
  });

  test('Message persistence across page reload', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });

    // Send message
    const persistentMessage = 'This should persist after reload';
    await page.getByTestId('chat-input').fill(persistentMessage);
    await page.getByTestId('send-message-btn').click();

    // Verify message appears
    await expect(page.getByTestId('messages-container')).toContainText(persistentMessage, { timeout: 10000 });

    // Reload page and navigate back to chat
    await page.reload();
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    
    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });

    // **CRITICAL: Verify message persisted**
    await expect(page.getByTestId('messages-container')).toContainText(persistentMessage, { 
      timeout: 10000 
    });
  });

  test('Thread switching preserves individual conversation state', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    // Wait for chat to load
    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('.new-thread-btn')).toBeVisible({ timeout: 5000 });

    // Send message in first thread
    const thread1Message = 'Message in thread 1';
    await page.getByTestId('chat-input').fill(thread1Message);
    await page.getByTestId('send-message-btn').click();
    
    await expect(page.getByTestId('messages-container')).toContainText(thread1Message, { timeout: 10000 });

    // Create new thread
    await page.locator('.new-thread-btn').click();
    
    // Wait for new thread to load (should be empty)
    await expect(page.getByTestId('messages-container')).not.toContainText(thread1Message, { timeout: 5000 });

    // Send message in second thread
    const thread2Message = 'Message in thread 2';
    await page.getByTestId('chat-input').fill(thread2Message);
    await page.getByTestId('send-message-btn').click();
    
    await expect(page.getByTestId('messages-container')).toContainText(thread2Message, { timeout: 10000 });

    // Switch back to first thread
    const firstThreadRow = page.locator('.thread-list .thread-row').first();
    await firstThreadRow.click();

    // **CRITICAL: Verify first thread's message is restored**
    await expect(page.getByTestId('messages-container')).toContainText(thread1Message, { timeout: 5000 });
    await expect(page.getByTestId('messages-container')).not.toContainText(thread2Message);
  });
});
