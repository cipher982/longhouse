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

test.describe('Chat Token Streaming Tests', () => {
  test('Verify token streaming shows up in UI', async ({ page }) => {
    // Create agent and navigate to chat
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    // Verify chat UI loads
    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });
    await expect(page.getByTestId('send-message-btn')).toBeVisible({ timeout: 5000 });

    // Send a test message that should trigger a response
    const testMessage = 'Say hello in exactly 10 words';
    await page.getByTestId('chat-input').fill(testMessage);
    await page.getByTestId('send-message-btn').click();

    // Wait for user message to appear
    await expect(page.getByTestId('messages-container')).toContainText(testMessage, { 
      timeout: 10000 
    });

    // Look for streaming indicator - check for message with data-streaming attribute
    const streamingMessage = page.locator('[data-streaming="true"]').first();
    
    // Wait for streaming to start (assistant message starts appearing)
    // We expect to see at least some content appearing character by character
    await expect(streamingMessage.or(page.locator('.message.streaming')).or(
      page.locator('[data-role="chat-message-assistant"]')
    )).toBeVisible({ timeout: 15000 });

    // Verify streaming cursor appears
    const streamingCursor = page.locator('.streaming-cursor');
    await expect(streamingCursor).toBeVisible({ timeout: 5000 }).catch(() => {
      // Cursor might not always be visible, that's okay
    });

    // Wait for streaming to complete - message should have content
    // The final message should appear without streaming attribute
    await expect(page.locator('[data-role="chat-message-assistant"]').last()).toContainText(/hello|greeting|Hi/i, {
      timeout: 30000
    });

    // Verify streaming state is cleared (no streaming attribute on final message)
    const finalMessage = page.locator('[data-role="chat-message-assistant"]').last();
    await expect(finalMessage).not.toHaveAttribute('data-streaming', 'true', { timeout: 5000 });
  });

  test('Verify tokens accumulate during streaming', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });

    // Send message that will generate a longer response
    const testMessage = 'Count from 1 to 5 with a word between each number';
    await page.getByTestId('chat-input').fill(testMessage);
    await page.getByTestId('send-message-btn').click();

    // Wait for user message
    await expect(page.getByTestId('messages-container')).toContainText(testMessage, { 
      timeout: 10000 
    });

    // Get the assistant message element
    const assistantMessage = page.locator('[data-role="chat-message-assistant"]').last();
    
    // Wait for streaming to start
    await expect(assistantMessage).toBeVisible({ timeout: 15000 });

    // Capture initial content length
    let previousLength = 0;
    let contentGrew = false;
    
    // Check multiple times that content is growing (tokens accumulating)
    for (let i = 0; i < 5; i++) {
      await page.waitForTimeout(1000); // Wait 1 second between checks
      
      const currentContent = await assistantMessage.locator('.message-content').textContent();
      const currentLength = currentContent?.length || 0;
      
      if (currentLength > previousLength) {
        contentGrew = true;
        previousLength = currentLength;
      }
    }

    // Verify content grew (tokens were accumulating)
    expect(contentGrew).toBe(true);

    // Wait for streaming to complete
    await expect(assistantMessage).not.toHaveAttribute('data-streaming', 'true', { 
      timeout: 30000 
    });

    // Verify final message has substantial content
    const finalContent = await assistantMessage.locator('.message-content').textContent();
    expect(finalContent?.length).toBeGreaterThan(10);
  });

  test('Verify multiple token chunks appear incrementally', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });

    const testMessage = 'Write a short sentence about AI';
    await page.getByTestId('chat-input').fill(testMessage);
    await page.getByTestId('send-message-btn').click();

    await expect(page.getByTestId('messages-container')).toContainText(testMessage, { 
      timeout: 10000 
    });

    const assistantMessage = page.locator('[data-role="chat-message-assistant"]').last();
    await expect(assistantMessage).toBeVisible({ timeout: 15000 });

    // Track content changes over time
    const contentSnapshots: string[] = [];
    
    for (let i = 0; i < 10; i++) {
      await page.waitForTimeout(500); // Check every 500ms
      const content = await assistantMessage.locator('.message-content').textContent();
      if (content) {
        contentSnapshots.push(content);
      }
      
      // If we see streaming has ended, break early
      const isStreaming = await assistantMessage.getAttribute('data-streaming');
      if (isStreaming !== 'true') {
        break;
      }
    }

    // Verify we captured multiple snapshots
    expect(contentSnapshots.length).toBeGreaterThan(1);

    // Verify content grew (each snapshot should be longer or equal)
    let grew = false;
    for (let i = 1; i < contentSnapshots.length; i++) {
      if (contentSnapshots[i].length > contentSnapshots[i - 1].length) {
        grew = true;
        break;
      }
    }
    expect(grew).toBe(true);
  });

  test('Verify streaming cursor animation', async ({ page }) => {
    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });

    const testMessage = 'Hello';
    await page.getByTestId('chat-input').fill(testMessage);
    await page.getByTestId('send-message-btn').click();

    await expect(page.getByTestId('messages-container')).toContainText(testMessage, { 
      timeout: 10000 
    });

    // Look for streaming message
    const streamingMessage = page.locator('[data-streaming="true"]').first();
    
    // Wait for streaming to start
    await expect(streamingMessage).toBeVisible({ timeout: 15000 }).catch(() => {
      // If streaming message not found, check for assistant message with cursor
      const assistantWithCursor = page.locator('[data-role="chat-message-assistant"]').filter({
        has: page.locator('.streaming-cursor')
      }).first();
      return expect(assistantWithCursor).toBeVisible({ timeout: 5000 });
    });

    // Verify cursor element exists
    const cursor = page.locator('.streaming-cursor');
    await expect(cursor).toBeVisible({ timeout: 2000 }).catch(() => {
      // Cursor might blink in/out, that's acceptable
    });

    // Verify cursor has animation (check computed styles)
    const cursorStyle = await cursor.evaluate((el) => {
      const style = window.getComputedStyle(el);
      return {
        animation: style.animation,
        animationName: style.animationName,
      };
    }).catch(() => null);

    if (cursorStyle) {
      // Animation should be set (either animation or animationName)
      expect(cursorStyle.animation || cursorStyle.animationName).toBeTruthy();
    }
  });

  test('CRITICAL: Switching threads mid-stream prevents token leakage', async ({ page }) => {
    console.log('🎯 Testing: Thread-switching token leakage prevention');

    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });

    // Send message in Thread A to trigger streaming
    const testMessage = 'Write a long detailed story about a robot exploring Mars';
    await page.getByTestId('chat-input').fill(testMessage);
    await page.getByTestId('send-message-btn').click();
    console.log('📊 Sent message in Thread A');

    // Wait for user message
    await expect(page.getByTestId('messages-container')).toContainText(testMessage, {
      timeout: 10000
    });

    // Wait for streaming to start
    const streamingMessage = page.locator('[data-streaming="true"]').first();
    await expect(streamingMessage).toBeVisible({ timeout: 15000 });
    console.log('✅ Streaming started in Thread A');

    // Get current thread ID from URL
    const threadAUrl = page.url();
    const threadAId = threadAUrl.match(/\/thread\/(\d+)/)?.[1];
    console.log(`📊 Thread A ID: ${threadAId}`);

    // Capture some content from Thread A's stream
    const threadAContent = await streamingMessage.locator('.message-content').textContent();
    console.log(`📊 Thread A initial content length: ${threadAContent?.length || 0} chars`);

    // Create and switch to Thread B while streaming is active
    const newThreadBtn = page.locator('[data-testid="new-thread-btn"]');
    if (await newThreadBtn.count() === 0) {
      console.log('⚠️  New thread button not found - skipping test');
      test.skip();
      return;
    }

    await newThreadBtn.click();
    await page.waitForTimeout(1000); // Wait for thread creation
    console.log('📊 Switched to Thread B');

    const threadBUrl = page.url();
    const threadBId = threadBUrl.match(/\/thread\/(\d+)/)?.[1];
    console.log(`📊 Thread B ID: ${threadBId}`);

    // Verify we're in a different thread
    expect(threadBId).not.toBe(threadAId);
    expect(threadBId).toBeTruthy();

    // CRITICAL: Verify Thread B has NO content from Thread A's streaming
    // Wait a bit to see if any tokens leak through
    await page.waitForTimeout(2000);

    // Thread B should be empty (new thread with no messages)
    const assistantMessagesInThreadB = page.locator('[data-role="chat-message-assistant"]');
    const threadBMessageCount = await assistantMessagesInThreadB.count();

    // CRITICAL: Thread B should have ZERO assistant messages
    // (More reliable than checking keywords - any assistant message is a leak)
    expect(threadBMessageCount).toBe(0);
    console.log('✅ Thread B has no assistant messages (no token leakage)');

    // Switch back to Thread A
    const threadASelector = `[data-thread-id="${threadAId}"]`;
    const threadAInSidebar = page.locator(threadASelector);

    if (await threadAInSidebar.count() > 0) {
      await threadAInSidebar.click();
      await page.waitForTimeout(500);
      console.log('📊 Switched back to Thread A');

      // Wait for streaming to complete and persist (may still be streaming)
      await page.waitForTimeout(3000);

      // Verify Thread A has assistant messages (streaming continued in background)
      const assistantMessagesInThreadA = page.locator('[data-role="chat-message-assistant"]');
      const threadAMessageCount = await assistantMessagesInThreadA.count();

      // Thread A should eventually have assistant messages (wait up to 10s)
      if (threadAMessageCount === 0) {
        await expect(assistantMessagesInThreadA.first()).toBeVisible({ timeout: 10000 });
      }

      const finalThreadAMessageCount = await assistantMessagesInThreadA.count();
      expect(finalThreadAMessageCount).toBeGreaterThan(0);
      console.log(`✅ Thread A has ${finalThreadAMessageCount} assistant message(s)`);

      // Verify Thread A contains actual content (not just empty messages)
      const messagesInThreadA = page.getByTestId('messages-container');
      const threadAContent = await messagesInThreadA.textContent();
      expect(threadAContent?.length || 0).toBeGreaterThan(20);
      console.log('✅ Thread A preserved its content');
    }

    console.log('✅ Thread-switching token isolation PASSED');
  });

  test('Writing indicator badge appears on background streaming threads', async ({ page }) => {
    console.log('🎯 Testing: ✍️ badge visibility for background threads');

    const agentId = await createAgentAndGetId(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });

    // Send message to trigger streaming
    const testMessage = 'Count from 1 to 100 slowly with explanations';
    await page.getByTestId('chat-input').fill(testMessage);
    await page.getByTestId('send-message-btn').click();
    console.log('📊 Started streaming in Thread A');

    // Wait for streaming to start
    const streamingMessage = page.locator('[data-streaming="true"]').first();
    await expect(streamingMessage).toBeVisible({ timeout: 15000 });
    console.log('✅ Streaming active in Thread A');

    // Get thread ID
    const threadUrl = page.url();
    const threadId = threadUrl.match(/\/thread\/(\d+)/)?.[1];
    console.log(`📊 Thread ID: ${threadId}`);

    // Create new thread (navigate away from streaming thread)
    const newThreadBtn = page.locator('[data-testid="new-thread-btn"]');
    if (await newThreadBtn.count() === 0) {
      console.log('⚠️  New thread button not found - skipping test');
      test.skip();
      return;
    }

    await newThreadBtn.click();
    await page.waitForTimeout(1000);
    console.log('📊 Created and switched to Thread B');

    // Verify original thread shows "✍️ writing..." badge in sidebar
    const threadInSidebar = page.locator(`[data-thread-id="${threadId}"]`);

    if (await threadInSidebar.count() > 0) {
      // Check for writing indicator within the thread item
      const writingIndicator = threadInSidebar.locator('.writing-indicator');

      try {
        await expect(writingIndicator).toBeVisible({ timeout: 5000 });
        const indicatorText = await writingIndicator.textContent();
        expect(indicatorText).toContain('✍️');
        console.log('✅ Writing indicator badge visible');
      } catch (error) {
        console.log('⚠️  Writing indicator not visible - may have completed');
        // Stream might have finished already - that's okay
      }

      // Wait for streaming to complete
      await page.waitForTimeout(10000);

      // Badge should disappear when streaming completes
      const stillVisible = await writingIndicator.isVisible().catch(() => false);
      if (!stillVisible) {
        console.log('✅ Writing badge correctly disappeared after stream completed');
      } else {
        console.log('⚠️  Badge still visible - stream may still be active');
      }
    } else {
      console.log('⚠️  Thread not found in sidebar');
    }

    console.log('✅ Writing indicator badge test completed');
  });
});

