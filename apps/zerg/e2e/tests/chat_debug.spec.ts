import { test, expect, type Page } from './fixtures';

// Reset DB before each test to keep agent/thread ids predictable
test.beforeEach(async ({ request }) => {
  await request.post('http://localhost:8001/admin/reset-database');
});

async function createAgentAndGetId(page: Page): Promise<string> {
  await page.goto('/');
  await page.locator('[data-testid="create-agent-btn"]').click();
  const row = page.locator('tr[data-agent-id]').first();
  await expect(row).toBeVisible();
  return (await row.getAttribute('data-agent-id')) as string;
}

test.describe('Chat Debug - Single Message Flow', () => {
  test('Debug single message send with verbose logging', async ({ page }) => {
    console.log('🧪 Starting chat debug test...');

    // Enable browser console logging
    page.on('console', msg => {
      console.log(`[BROWSER] ${msg.type()}: ${msg.text()}`);
    });

    // Create agent and navigate to chat
    console.log('📋 Step 1: Creating agent...');
    const agentId = await createAgentAndGetId(page);
    console.log(`📋 Agent created with ID: ${agentId}`);

    console.log('📋 Step 2: Navigating to chat...');
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    // Verify chat UI loads
    console.log('📋 Step 3: Verifying chat UI elements...');
    await expect(page.locator('.chat-input')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('.send-button')).toBeVisible({ timeout: 5000 });
    console.log('✅ Chat UI elements are visible');

    // Check initial state of messages container
    const messagesContainer = page.locator('.messages-container');
    const initialContent = await messagesContainer.textContent();
    console.log(`📋 Initial messages container content: "${initialContent}"`);

    // Send a test message
    const testMessage = 'Debug test message';
    console.log(`📋 Step 4: Sending message: "${testMessage}"`);

    await page.locator('.chat-input').fill(testMessage);
    console.log('✅ Message filled in chat input');

    // Check if input has the text
    const inputValue = await page.locator('.chat-input').inputValue();
    console.log(`📋 Input value after fill: "${inputValue}"`);

    // Click send button
    console.log('📋 Step 5: Clicking send button...');
    await page.locator('.send-button').click();
    console.log('✅ Send button clicked');

    // Wait a moment for any immediate updates
    await page.waitForTimeout(1000);

    // Check messages container immediately after send
    const contentAfterSend = await messagesContainer.textContent();
    console.log(`📋 Messages container content after send: "${contentAfterSend}"`);

    // Check if input was cleared (indicates message was processed)
    const inputAfterSend = await page.locator('.chat-input').inputValue();
    console.log(`📋 Input value after send: "${inputAfterSend}"`);

    // Wait a bit longer for any async updates
    console.log('📋 Step 6: Waiting for async updates...');
    await page.waitForTimeout(3000);

    const contentAfterWait = await messagesContainer.textContent();
    console.log(`📋 Messages container content after 3s wait: "${contentAfterWait}"`);

    // Check the DOM structure of messages container
    const innerHTML = await messagesContainer.innerHTML();
    console.log(`📋 Messages container HTML: ${innerHTML}`);

    // Look for any message elements
    const messageElements = page.locator('.message, .user-message, .assistant-message, [class*="message"]');
    const messageCount = await messageElements.count();
    console.log(`📋 Found ${messageCount} message elements`);

    if (messageCount > 0) {
      for (let i = 0; i < messageCount; i++) {
        const messageText = await messageElements.nth(i).textContent();
        const messageClass = await messageElements.nth(i).getAttribute('class');
        console.log(`📋 Message ${i}: class="${messageClass}", text="${messageText}"`);
      }
    }

    // Try to find the message with more specific selectors
    const possibleSelectors = [
      '.message.user',
      '.user-message',
      '.message[data-role="user"]',
      '.chat-message',
      '.thread-message'
    ];

    for (const selector of possibleSelectors) {
      const elements = page.locator(selector);
      const count = await elements.count();
      if (count > 0) {
        const text = await elements.first().textContent();
        console.log(`📋 Found message with selector "${selector}": "${text}"`);
      }
    }

    // Check if there are any error indicators
    const errorElements = page.locator('.error, .failed, [class*="error"]');
    const errorCount = await errorElements.count();
    console.log(`📋 Found ${errorCount} error elements`);

    console.log('📋 Step 7: Final verification attempt...');

    // **CRITICAL: Verify message actually appears in the UI**
    // This should pass if our fix worked
    try {
      await expect(messagesContainer).toContainText(testMessage, { timeout: 2000 });
      console.log('🎉 SUCCESS: Message found in UI!');
    } catch (error) {
      console.log('❌ FAILED: Message not found in UI');
      console.log(`❌ Error: ${error.message}`);

      // Log final state for debugging
      const finalContent = await messagesContainer.textContent();
      const finalHTML = await messagesContainer.innerHTML();
      console.log(`📋 Final container text: "${finalContent}"`);
      console.log(`📋 Final container HTML: ${finalHTML}`);

      // Re-throw to fail the test
      throw error;
    }
  });
});
