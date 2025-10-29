import { test, expect, type Page } from './fixtures';

/**
 * E2E test for chat message ordering fix
 *
 * CONTEXT:
 * - Previously, optimistic messages appeared at the top and jumped to the bottom when the server responded
 * - The fix uses timestamp-based sorting (compareMessagesChronologically in ChatPage.tsx)
 * - Messages should stay anchored to the bottom throughout the optimistic→server flow
 *
 * WHAT WE'RE TESTING:
 * 1. Single message stays at bottom during optimistic→server flow
 * 2. Multiple rapid messages maintain chronological order
 * 3. Messages remain stable (no jumping) when server responds
 * 4. DOM order matches chronological order
 */

// Reset DB before each test to keep agent/thread ids predictable
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database');
});

async function createAgentAndNavigateToChat(page: Page): Promise<string> {
  await page.goto('/');
  await page.locator('[data-testid="create-agent-btn"]').click();
  const row = page.locator('tr[data-agent-id]').first();
  await expect(row).toBeVisible();
  const agentId = (await row.getAttribute('data-agent-id')) as string;

  // Navigate to chat
  await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
  await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });
  await expect(page.getByTestId('send-message-btn')).toBeVisible({ timeout: 5000 });

  return agentId;
}

/**
 * Get all message elements in DOM order
 */
async function getMessagesInDomOrder(page: Page): Promise<string[]> {
  const messages = page.locator('[data-role^="chat-message-"]');
  const count = await messages.count();
  const contents: string[] = [];

  for (let i = 0; i < count; i++) {
    const messageContent = messages.nth(i).locator('.message-content');
    const text = await messageContent.textContent();
    if (text) {
      contents.push(text.trim());
    }
  }

  return contents;
}

/**
 * Get the position (Y coordinate) of a specific message in the viewport
 */
async function getMessagePosition(page: Page, messageText: string): Promise<number | null> {
  const messages = page.locator('[data-role^="chat-message-"]');
  const count = await messages.count();

  for (let i = 0; i < count; i++) {
    const message = messages.nth(i);
    const content = await message.locator('.message-content').textContent();
    if (content?.trim() === messageText) {
      const box = await message.boundingBox();
      return box?.y || null;
    }
  }

  return null;
}

test.describe('Chat Message Ordering - Optimistic Updates', () => {
  test('Single message stays anchored at bottom during optimistic→server flow', async ({ page }) => {
    await createAgentAndNavigateToChat(page);

    const testMessage = 'Test message for ordering verification';

    // Step 1: Send the message
    await page.getByTestId('chat-input').fill(testMessage);
    await page.getByTestId('send-message-btn').click();

    // Step 2: IMMEDIATELY verify the optimistic message appears at the bottom
    // (before server responds)
    await expect(page.getByTestId('messages-container')).toContainText(testMessage, {
      timeout: 1000
    });

    // Get initial position of the message (should be at bottom)
    const initialPosition = await getMessagePosition(page, testMessage);
    expect(initialPosition).not.toBeNull();

    // Verify message is in the DOM
    const messagesBeforeServerResponse = await getMessagesInDomOrder(page);
    expect(messagesBeforeServerResponse).toContain(testMessage);
    expect(messagesBeforeServerResponse[messagesBeforeServerResponse.length - 1]).toBe(testMessage);

    // Step 3: Wait for server response (optimistic message gets replaced with real ID)
    // The message should NOT move or jump
    await page.waitForTimeout(2000); // Give server time to respond

    // Step 4: Verify message is STILL at the bottom and hasn't jumped
    const messagesAfterServerResponse = await getMessagesInDomOrder(page);
    expect(messagesAfterServerResponse).toContain(testMessage);
    expect(messagesAfterServerResponse[messagesAfterServerResponse.length - 1]).toBe(testMessage);

    // Verify position hasn't changed significantly (allowing for minor scroll adjustments)
    const finalPosition = await getMessagePosition(page, testMessage);
    expect(finalPosition).not.toBeNull();

    if (initialPosition && finalPosition) {
      // Position should be stable (within 50px tolerance for any layout shifts)
      const positionDifference = Math.abs(finalPosition - initialPosition);
      expect(positionDifference).toBeLessThan(50);
    }
  });

  test('Multiple rapid messages maintain chronological order', async ({ page }) => {
    await createAgentAndNavigateToChat(page);

    const messages = [
      'First message',
      'Second message',
      'Third message'
    ];

    // Send messages rapidly in sequence
    for (const message of messages) {
      await page.getByTestId('chat-input').fill(message);
      await page.getByTestId('send-message-btn').click();
      // Small delay to ensure messages are distinct but still rapid
      await page.waitForTimeout(100);
    }

    // Wait for all messages to appear
    for (const message of messages) {
      await expect(page.getByTestId('messages-container')).toContainText(message, {
        timeout: 5000
      });
    }

    // Verify chronological order in DOM
    const domOrder = await getMessagesInDomOrder(page);

    // Filter to only our test messages (ignore any system messages)
    const testMessagesInDom = domOrder.filter(msg => messages.includes(msg));

    expect(testMessagesInDom).toEqual(messages);

    // Verify each message appears exactly once
    for (const message of messages) {
      const occurrences = testMessagesInDom.filter(m => m === message).length;
      expect(occurrences).toBe(1);
    }
  });

  test('Optimistic message ordering is stable across multiple sends', async ({ page }) => {
    await createAgentAndNavigateToChat(page);

    // Send first message and wait for it to settle
    await page.getByTestId('chat-input').fill('Message 1');
    await page.getByTestId('send-message-btn').click();
    await expect(page.getByTestId('messages-container')).toContainText('Message 1', {
      timeout: 5000
    });
    await page.waitForTimeout(1000); // Let server respond

    // Send second message
    await page.getByTestId('chat-input').fill('Message 2');
    await page.getByTestId('send-message-btn').click();

    // Immediately verify order before server responds
    const immediateOrder = await getMessagesInDomOrder(page);
    const testMessages = immediateOrder.filter(m => m.startsWith('Message'));
    expect(testMessages).toEqual(['Message 1', 'Message 2']);

    // Wait for server response
    await page.waitForTimeout(2000);

    // Verify order is still correct after server response
    const finalOrder = await getMessagesInDomOrder(page);
    const finalTestMessages = finalOrder.filter(m => m.startsWith('Message'));
    expect(finalTestMessages).toEqual(['Message 1', 'Message 2']);
  });

  test('Message order persists across page reload', async ({ page }) => {
    const agentId = await createAgentAndNavigateToChat(page);

    const messages = ['Persistent message 1', 'Persistent message 2', 'Persistent message 3'];

    // Send multiple messages
    for (const message of messages) {
      await page.getByTestId('chat-input').fill(message);
      await page.getByTestId('send-message-btn').click();
      await page.waitForTimeout(500); // Ensure each message is distinct
    }

    // Wait for all messages to appear and server to respond
    for (const message of messages) {
      await expect(page.getByTestId('messages-container')).toContainText(message, {
        timeout: 5000
      });
    }
    await page.waitForTimeout(2000);

    // Get order before reload
    const orderBeforeReload = await getMessagesInDomOrder(page);
    const testMessagesBeforeReload = orderBeforeReload.filter(m => messages.includes(m));

    // Reload page and navigate back to chat
    await page.reload();
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });

    // Verify order after reload matches order before reload
    const orderAfterReload = await getMessagesInDomOrder(page);
    const testMessagesAfterReload = orderAfterReload.filter(m => messages.includes(m));

    expect(testMessagesAfterReload).toEqual(testMessagesBeforeReload);
    expect(testMessagesAfterReload).toEqual(messages);
  });

  test('Optimistic message does not appear at top then jump to bottom', async ({ page }) => {
    await createAgentAndNavigateToChat(page);

    const testMessage = 'No jumping message';

    // Track all positions of the message as it appears
    const positions: number[] = [];

    // Send message
    await page.getByTestId('chat-input').fill(testMessage);
    await page.getByTestId('send-message-btn').click();

    // Immediately check position (optimistic)
    await expect(page.getByTestId('messages-container')).toContainText(testMessage, {
      timeout: 1000
    });
    const pos1 = await getMessagePosition(page, testMessage);
    if (pos1) positions.push(pos1);

    // Check position after a short delay
    await page.waitForTimeout(500);
    const pos2 = await getMessagePosition(page, testMessage);
    if (pos2) positions.push(pos2);

    // Check position after server responds
    await page.waitForTimeout(1500);
    const pos3 = await getMessagePosition(page, testMessage);
    if (pos3) positions.push(pos3);

    // Verify all positions are similar (no large jumps)
    expect(positions.length).toBeGreaterThan(0);

    if (positions.length > 1) {
      for (let i = 1; i < positions.length; i++) {
        const difference = Math.abs(positions[i] - positions[i - 1]);
        // Allow for minor scroll adjustments but not large jumps (e.g., top→bottom)
        expect(difference).toBeLessThan(100);
      }
    }

    // Final verification: message should be at the bottom
    const finalOrder = await getMessagesInDomOrder(page);
    expect(finalOrder[finalOrder.length - 1]).toBe(testMessage);
  });

  test('Empty input does not create optimistic message', async ({ page }) => {
    await createAgentAndNavigateToChat(page);

    // Try to send empty message
    await page.getByTestId('chat-input').fill('');

    // Send button should be disabled
    const sendButton = page.getByTestId('send-message-btn');
    await expect(sendButton).toBeDisabled();

    // Even if we try to click it (shouldn't work), no message should appear
    const initialMessages = await getMessagesInDomOrder(page);
    const initialCount = initialMessages.length;

    // Try clicking anyway (should not work due to disabled state)
    try {
      await sendButton.click({ force: true, timeout: 1000 });
    } catch (e) {
      // Expected to fail
    }

    await page.waitForTimeout(1000);

    const finalMessages = await getMessagesInDomOrder(page);
    expect(finalMessages.length).toBe(initialCount);
  });

  test('Whitespace-only input does not create optimistic message', async ({ page }) => {
    await createAgentAndNavigateToChat(page);

    // Try to send whitespace-only message
    await page.getByTestId('chat-input').fill('   ');

    // Send button should be disabled
    const sendButton = page.getByTestId('send-message-btn');
    await expect(sendButton).toBeDisabled();

    const initialMessages = await getMessagesInDomOrder(page);
    const initialCount = initialMessages.length;

    await page.waitForTimeout(1000);

    const finalMessages = await getMessagesInDomOrder(page);
    expect(finalMessages.length).toBe(initialCount);
  });
});

test.describe('Chat Message Ordering - Thread Switching', () => {
  test('Message order is preserved when switching between threads', async ({ page }) => {
    await createAgentAndNavigateToChat(page);

    // Send messages in first thread
    const thread1Messages = ['Thread 1 - Message A', 'Thread 1 - Message B'];
    for (const message of thread1Messages) {
      await page.getByTestId('chat-input').fill(message);
      await page.getByTestId('send-message-btn').click();
      await page.waitForTimeout(500);
    }

    // Wait for messages to appear
    for (const message of thread1Messages) {
      await expect(page.getByTestId('messages-container')).toContainText(message, {
        timeout: 5000
      });
    }

    // Create new thread
    await page.locator('.new-thread-btn').click();
    await page.waitForTimeout(1000);

    // Send messages in second thread
    const thread2Messages = ['Thread 2 - Message X', 'Thread 2 - Message Y'];
    for (const message of thread2Messages) {
      await page.getByTestId('chat-input').fill(message);
      await page.getByTestId('send-message-btn').click();
      await page.waitForTimeout(500);
    }

    // Wait for messages to appear
    for (const message of thread2Messages) {
      await expect(page.getByTestId('messages-container')).toContainText(message, {
        timeout: 5000
      });
    }

    // Verify thread 2 order
    const thread2Order = await getMessagesInDomOrder(page);
    const thread2TestMessages = thread2Order.filter(m => m.startsWith('Thread 2'));
    expect(thread2TestMessages).toEqual(thread2Messages);

    // Switch back to first thread
    const firstThreadRow = page.locator('.thread-list .thread-item').first();
    await firstThreadRow.click();
    await page.waitForTimeout(1000);

    // Verify thread 1 order is still correct
    const thread1Order = await getMessagesInDomOrder(page);
    const thread1TestMessages = thread1Order.filter(m => m.startsWith('Thread 1'));
    expect(thread1TestMessages).toEqual(thread1Messages);

    // Should not see thread 2 messages
    const hasThread2Messages = thread1Order.some(m => m.startsWith('Thread 2'));
    expect(hasThread2Messages).toBe(false);
  });
});
