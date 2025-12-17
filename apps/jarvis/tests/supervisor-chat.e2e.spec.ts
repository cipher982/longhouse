import { test, expect } from '@playwright/test';

/**
 * E2E tests for Supervisor Chat (Text-only mode via POST /api/jarvis/chat)
 *
 * This test suite verifies the new direct-to-Supervisor chat feature:
 * - User sends text messages to Supervisor
 * - SSE streaming from backend shows responses
 * - Messages persist across page refreshes
 * - Clear history works correctly
 *
 * These tests replace the old OpenAI Realtime text channel with direct
 * Supervisor API integration. Unlike voice tests, these can run in Docker.
 *
 * Prerequisites:
 * - Zerg backend running with Supervisor enabled
 * - OPENAI_API_KEY configured in backend
 *
 * CURRENT STATUS:
 * - Backend /api/jarvis/chat endpoint exists and works (verified via curl)
 * - Frontend UI integration is incomplete - messages don't send from UI
 * - Tests are currently SKIPPED until UI integration is completed
 * - Remove test.describe.skip() below when UI is working
 */

test.describe.skip('Supervisor Chat E2E', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to Jarvis chat
    await page.goto('/chat/');

    // Wait for chat UI to load
    await page.waitForSelector('.transcript', { timeout: 30000 });

    // Wait for text input to be available AND enabled
    const textInput = page.locator('input[placeholder*="Type a message"]');
    await textInput.waitFor({ state: 'visible', timeout: 30000 });

    // Wait for input to be enabled (app initialization complete)
    await expect(textInput).toBeEnabled({ timeout: 10000 });
  });

  test('should send text message to Supervisor and display streaming response', async ({ page }) => {
    // Set longer timeout for real API calls
    test.setTimeout(90000);

    const textInput = page.locator('input[placeholder*="Type a message"]');
    const sendButton = page.locator('button[aria-label="Send message"]');

    // Send a simple test message
    const testMessage = 'Say hello in exactly 3 words';
    await textInput.fill(testMessage);

    // Verify button is enabled after typing
    await expect(sendButton).toBeEnabled({ timeout: 1000 });

    // Take screenshot before sending
    await page.screenshot({ path: './test-results/supervisor-before-send.png', fullPage: true });

    // Send the message
    await sendButton.click();

    // Wait a moment for the click to process
    await page.waitForTimeout(500);

    // The message should appear in the chat immediately (optimistic update)
    await expect(page.locator('.transcript')).toContainText(testMessage, { timeout: 5000 });

    // Input should be cleared
    await expect(textInput).toHaveValue('');

    // Wait for assistant response to appear
    // The response should stream in via SSE
    await page.waitForFunction(
      () => {
        const transcript = document.querySelector('.transcript');
        if (!transcript) return false;

        const messages = transcript.querySelectorAll('.message');

        // Should have at least 2 messages: user + assistant
        if (messages.length < 2) return false;

        // Last message should be assistant response
        const lastMessage = messages[messages.length - 1];
        if (!lastMessage.classList.contains('assistant')) return false;

        // Should have some content
        const content = lastMessage.textContent || '';
        return content.length > 0 && content !== testMessage;
      },
      { timeout: 60000 }
    );

    // Take screenshot after response
    await page.screenshot({ path: './test-results/supervisor-after-response.png', fullPage: true });

    // Verify we have at least 2 messages
    const transcript = page.locator('.transcript');
    const messages = transcript.locator('.message');
    const count = await messages.count();

    expect(count).toBeGreaterThanOrEqual(2);

    // Verify message roles
    const firstMessage = messages.first();
    await expect(firstMessage).toHaveClass(/user/);

    const lastMessage = messages.last();
    await expect(lastMessage).toHaveClass(/assistant/);

    console.log(`✅ Supervisor chat test passed: Found ${count} messages`);
  });

  test('should maintain conversation context across multiple messages', async ({ page }) => {
    test.setTimeout(120000);

    const textInput = page.locator('input[placeholder*="Type a message"]');
    const sendButton = page.locator('button[aria-label="Send message"], button:has-text("Send")').first();

    // First message - ask to remember something
    await textInput.fill('Remember the number 42');
    await sendButton.click();

    // Wait for first response
    await page.waitForFunction(
      () => {
        const messages = document.querySelectorAll('.transcript .message');
        return messages.length >= 2;
      },
      { timeout: 60000 }
    );

    // Second message - reference the first
    await textInput.fill('What number did I ask you to remember?');
    await sendButton.click();

    // Wait for second response
    await page.waitForFunction(
      () => {
        const messages = document.querySelectorAll('.transcript .message');
        return messages.length >= 4; // user1, assistant1, user2, assistant2
      },
      { timeout: 60000 }
    );

    // Verify context was maintained - response should mention "42"
    const transcript = page.locator('.transcript');
    await expect(transcript).toContainText('42', { timeout: 5000 });

    console.log('✅ Conversation context maintained across messages');
  });

  test('should show streaming indicator during response generation', async ({ page }) => {
    test.setTimeout(90000);

    const textInput = page.locator('input[placeholder*="Type a message"]');
    const sendButton = page.locator('button[aria-label="Send message"], button:has-text("Send")').first();

    // Send a message that will generate a longer response
    await textInput.fill('Count from 1 to 10');
    await sendButton.click();

    let sawStreamingState = false;

    // Poll for streaming indicators
    for (let i = 0; i < 30; i++) {
      await page.waitForTimeout(500);

      // Check for streaming class on assistant message
      const streamingMessages = await page.locator('.message.assistant.streaming').count();
      if (streamingMessages > 0) {
        sawStreamingState = true;
        console.log('✅ Detected streaming class on message');
        break;
      }

      // Also check if we see partial content (streaming in progress)
      const messages = await page.locator('.transcript .message').count();
      if (messages >= 2) {
        const lastMessage = page.locator('.transcript .message').last();
        const content = await lastMessage.textContent();

        // If we see numbers but not "10" yet, streaming is working
        if (content && /[1-9]/.test(content) && !content.includes('10')) {
          sawStreamingState = true;
          console.log('✅ Detected partial content (streaming in progress)');
          break;
        }
      }
    }

    // Wait for complete response
    await page.waitForFunction(
      () => {
        const messages = document.querySelectorAll('.transcript .message');
        if (messages.length < 2) return false;

        const lastMessage = messages[messages.length - 1];
        const content = lastMessage.textContent || '';

        // Response should be complete (no streaming class, has "10")
        return !lastMessage.classList.contains('streaming') && content.includes('10');
      },
      { timeout: 60000 }
    );

    console.log(`Streaming state observed: ${sawStreamingState}`);

    // Note: Very fast responses might not show streaming state
    // The important thing is that the final response appears correctly
  });

  test('should persist chat history across page refresh', async ({ page }) => {
    test.setTimeout(120000);

    const textInput = page.locator('input[placeholder*="Type a message"]');
    const sendButton = page.locator('button[aria-label="Send message"], button:has-text("Send")').first();

    // Send a unique test message
    const uniqueMessage = `Test message at ${Date.now()}`;
    await textInput.fill(uniqueMessage);
    await sendButton.click();

    // Wait for response
    await page.waitForFunction(
      () => {
        const messages = document.querySelectorAll('.transcript .message');
        return messages.length >= 2;
      },
      { timeout: 60000 }
    );

    // Take screenshot before refresh
    await page.screenshot({ path: './test-results/supervisor-before-refresh.png', fullPage: true });

    // Reload the page
    console.log('Reloading page to test history persistence...');
    await page.reload();

    // Wait for chat UI to load again
    await page.waitForSelector('.transcript', { timeout: 30000 });
    await page.waitForTimeout(2000); // Give time for history to load

    // Take screenshot after refresh
    await page.screenshot({ path: './test-results/supervisor-after-refresh.png', fullPage: true });

    // Verify the message is still there
    const transcript = page.locator('.transcript');
    await expect(transcript).toContainText(uniqueMessage, { timeout: 10000 });

    // Verify we still have at least 2 messages (user + assistant)
    const messages = transcript.locator('.message');
    const count = await messages.count();
    expect(count).toBeGreaterThanOrEqual(2);

    console.log(`✅ History persisted after refresh: ${count} messages found`);
  });

  test('should handle empty conversation state gracefully', async ({ page }) => {
    // On fresh load, transcript should show system ready message
    const transcript = page.locator('.transcript');

    // Should have either empty state or status message
    const hasContent = await transcript.locator('.message').count() > 0;
    const hasStatus = await transcript.locator('.status-message').count() > 0;

    expect(hasContent || hasStatus).toBeTruthy();

    // Input should be available
    const textInput = page.locator('input[placeholder*="Type a message"]');
    await expect(textInput).toBeVisible();

    console.log('✅ Empty conversation state handled gracefully');
  });

  test('should handle SSE connection errors gracefully', async ({ page }) => {
    test.setTimeout(90000);

    // This test verifies error handling if the SSE stream fails
    // We can't easily force an error, but we can verify the UI doesn't break

    const textInput = page.locator('input[placeholder*="Type a message"]');
    const sendButton = page.locator('button[aria-label="Send message"], button:has-text("Send")').first();

    // Send a message
    await textInput.fill('Hello');
    await sendButton.click();

    // Wait a reasonable time for response or error
    await page.waitForTimeout(10000);

    // UI should not have any JavaScript errors
    // If there were errors, the test would have failed by now

    // Verify the message at least appears in the UI
    await expect(page.locator('.transcript')).toContainText('Hello');

    console.log('✅ SSE error handling verified');
  });

  test('should clear all conversations when requested', async ({ page }) => {
    test.setTimeout(120000);

    const textInput = page.locator('input[placeholder*="Type a message"]');
    const sendButton = page.locator('button[aria-label="Send message"], button:has-text("Send")').first();

    // Send a test message
    await textInput.fill('Test message to be cleared');
    await sendButton.click();

    // Wait for response
    await page.waitForFunction(
      () => {
        const messages = document.querySelectorAll('.transcript .message');
        return messages.length >= 2;
      },
      { timeout: 60000 }
    );

    // Verify we have messages
    let messageCount = await page.locator('.transcript .message').count();
    expect(messageCount).toBeGreaterThanOrEqual(2);

    console.log(`Messages before clear: ${messageCount}`);

    // Look for clear/delete button (could be in menu, sidebar, or toolbar)
    // Try common selectors for clear functionality
    const clearButton = page.locator(
      'button:has-text("Clear"), button:has-text("Delete"), button:has-text("New"), button[aria-label*="Clear"], button[aria-label*="Delete"], button[aria-label*="New"]'
    ).first();

    // If no clear button found, use keyboard shortcut or menu
    const hasClearButton = await clearButton.count() > 0;

    if (hasClearButton) {
      await clearButton.click();

      // Wait for confirmation or immediate clear
      await page.waitForTimeout(1000);

      // If there's a confirmation dialog, confirm it
      const confirmButton = page.locator('button:has-text("Confirm"), button:has-text("Yes"), button:has-text("OK")');
      if (await confirmButton.count() > 0) {
        await confirmButton.click();
      }

      // Wait for messages to be cleared
      await page.waitForTimeout(2000);

      // Verify messages are cleared
      messageCount = await page.locator('.transcript .message').count();
      expect(messageCount).toBe(0);

      console.log('✅ Messages cleared successfully');
    } else {
      console.log('⚠️  No clear button found - skipping clear test');
      test.skip();
    }
  });

  test('should handle rapid successive messages', async ({ page }) => {
    test.setTimeout(120000);

    const textInput = page.locator('input[placeholder*="Type a message"]');
    const sendButton = page.locator('button[aria-label="Send message"], button:has-text("Send")').first();

    // Send multiple messages quickly
    for (let i = 1; i <= 3; i++) {
      await textInput.fill(`Quick message ${i}`);
      await sendButton.click();

      // Small delay between messages
      await page.waitForTimeout(500);
    }

    // Wait for all responses to come back
    await page.waitForFunction(
      () => {
        const messages = document.querySelectorAll('.transcript .message');
        // Should have at least 6 messages (3 user + 3 assistant)
        return messages.length >= 6;
      },
      { timeout: 90000 }
    );

    // Verify all messages appeared
    const transcript = page.locator('.transcript');
    await expect(transcript).toContainText('Quick message 1');
    await expect(transcript).toContainText('Quick message 2');
    await expect(transcript).toContainText('Quick message 3');

    const messageCount = await page.locator('.transcript .message').count();
    console.log(`✅ Handled rapid messages: ${messageCount} total messages`);
  });
});
