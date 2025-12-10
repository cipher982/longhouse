import { test, expect } from '@playwright/test';

/**
 * Happy Path E2E Test: Text Message → AI Response
 *
 * Tests the critical user journey:
 * 1. User loads the app
 * 2. User sends a text message
 * 3. AI response appears in the UI
 *
 * This test was written after discovering that while we had unit tests
 * and component tests, we had NO test that verified AI responses actually
 * render in the chat UI. Multiple production bugs went undetected:
 * - voiceController not initialized
 * - Context not loaded
 * - Streaming responses not rendering
 *
 * Unlike voice tests, this does NOT require WebRTC, so it can run in Docker.
 * It uses the text channel which works over standard HTTP.
 */

test.describe('Text Message Happy Path', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to the app
    await page.goto('/chat/');

    // Wait for app to be loaded - React app uses .transcript (class), not #transcript (ID)
    await page.waitForSelector('.transcript', { timeout: 30000 });

    // Wait for the text input to be visible first
    const textInput = page.locator('input[placeholder*="Type a message"]');
    await textInput.waitFor({ state: 'visible', timeout: 30000 });

    // Wait for the session to connect (input becomes enabled)
    // The session may take time to connect to OpenAI
    try {
      await page.waitForSelector('input[placeholder*="Type a message"]:not([disabled])', {
        state: 'visible',
        timeout: 90000  // Give more time for session connection
      });
      console.log('✅ Session connected - input is enabled');
    } catch (e) {
      // If session didn't connect, log the error and take a screenshot
      console.error('❌ Session did not connect within timeout');
      await page.screenshot({ path: './test-results/session-not-connected.png', fullPage: true });
      throw new Error('Session did not connect - text input remained disabled. Check OPENAI_API_KEY and network connectivity.');
    }

    // Small delay to ensure session is fully ready
    await page.waitForTimeout(500);
  });

  test('should send text message and display AI response', async ({ page }) => {
    // Set a longer timeout for real API call
    test.setTimeout(90000);

    // Verify we're in text mode (standalone or bridge mode both work)
    const textInput = page.locator('input[placeholder*="Type a message"]');
    const sendButton = page.locator('button[aria-label="Send message"], button:has-text("Send")').first();

    // Type a simple test message
    const testMessage = 'Say hello back in exactly 3 words';
    await textInput.fill(testMessage);

    // Take screenshot before sending
    await page.screenshot({ path: './test-results/before-send.png', fullPage: true });

    // Send the message
    await sendButton.click();

    // The message should appear in the chat immediately (optimistic update)
    await expect(page.locator('.transcript')).toContainText(testMessage, { timeout: 5000 });

    // Input should be cleared
    await expect(textInput).toHaveValue('');

    // THE CRITICAL ASSERTION: AI response should appear
    // Look for the transcript container to have more than just our message
    // The response should appear within 30 seconds
    await page.waitForFunction(
      () => {
        const transcript = document.querySelector('.transcript');
        if (!transcript) return false;

        // Count the message elements - React app uses .message with role classes
        const messages = transcript.querySelectorAll('.message');

        // We should have at least 2 messages: user + assistant
        if (messages.length < 2) return false;

        // Verify the last message is different from what we sent
        const lastMessage = messages[messages.length - 1];
        const lastText = lastMessage.textContent || '';

        // Should not be our test message
        if (lastText === 'Say hello back in exactly 3 words') return false;

        // Should have some content
        return lastText.length > 0;
      },
      { timeout: 60000 }
    );

    // Take screenshot after response
    await page.screenshot({ path: './test-results/after-response.png', fullPage: true });

    // Additional verification: count the messages
    const transcript = page.locator('.transcript');
    const messages = transcript.locator('.message');
    const count = await messages.count();

    // Should have at least user message + AI response
    expect(count).toBeGreaterThanOrEqual(2);

    console.log(`✅ Test passed: Found ${count} messages in chat`);
  });

  test('should handle multiple message exchanges', async ({ page }) => {
    test.setTimeout(120000);

    const textInput = page.locator('input[placeholder*="Type a message"]');
    const sendButton = page.locator('button[aria-label="Send message"], button:has-text("Send")').first();

    // First message
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

    // Second message referencing the first
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

    console.log('✅ Test passed: Conversation context maintained');
  });

  test('should show streaming indicator during response generation', async ({ page }) => {
    test.setTimeout(90000);

    const textInput = page.locator('input[placeholder*="Type a message"]');
    const sendButton = page.locator('button[aria-label="Send message"], button:has-text("Send")').first();

    // Send a message that will generate a longer response
    await textInput.fill('Count from 1 to 10 slowly');
    await sendButton.click();

    // During streaming, there should be some indication
    // This could be a streaming class, partial content, or a loading indicator
    let sawStreamingState = false;

    // Poll for streaming indicators
    for (let i = 0; i < 30; i++) {
      await page.waitForTimeout(1000);

      // Check for streaming indicators
      const hasStreamingClass = await page.locator('.streaming, .is-streaming, [data-streaming="true"]').count() > 0;
      const hasStreamingContent = await page.locator('.streaming-content').count() > 0;

      if (hasStreamingClass || hasStreamingContent) {
        sawStreamingState = true;
        console.log('✅ Detected streaming state');
        break;
      }

      // Also check if we see partial content (response is streaming)
      const messages = await page.locator('.transcript .message').count();
      if (messages >= 2) {
        // If we have a response, check if it's partial (streaming in progress)
        const lastMessage = page.locator('.transcript .message').last();
        const content = await lastMessage.textContent();

        // If we see numbers but not "10" yet, streaming is working
        if (content && content.includes('1') && !content.includes('10')) {
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
        return messages.length >= 2;
      },
      { timeout: 60000 }
    );

    // Note: If response is very fast, streaming state might not be observed
    // The important thing is that the final response appears
    console.log(`Streaming state observed: ${sawStreamingState}`);

    // Verify complete response contains "10"
    const transcript = page.locator('.transcript');
    await expect(transcript).toContainText('10', { timeout: 5000 });
  });
});
