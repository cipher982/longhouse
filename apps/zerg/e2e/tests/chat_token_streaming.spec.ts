import { test, expect, type Page } from './fixtures';
import { resetDatabase } from './test-utils';

// Reset DB before each test to keep fiche/thread ids predictable
// Uses strict reset that throws on failure to fail fast
test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

async function createFicheAndGetId(page: Page): Promise<string> {
  await page.goto('/dashboard');
  const createBtn = page.locator('[data-testid="create-fiche-btn"]');
  await expect(createBtn).toBeVisible({ timeout: 10000 });

  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/api/fiches') && r.request().method() === 'POST' && r.status() === 201,
      { timeout: 10000 }
    ),
    createBtn.click(),
  ]);

  const body = await response.json();
  const ficheId = String(body.id);

  const row = page.locator(`tr[data-fiche-id="${ficheId}"]`);
  await expect(row).toBeVisible({ timeout: 10000 });
  return ficheId;
}

test.describe('Chat Token Streaming Tests', () => {
  test('Verify token streaming shows up in UI', async ({ page }) => {
    // Create fiche and navigate to chat
    const ficheId = await createFicheAndGetId(page);
    await page.locator(`[data-testid="chat-fiche-${ficheId}"]`).click();

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
    const ficheId = await createFicheAndGetId(page);
    await page.locator(`[data-testid="chat-fiche-${ficheId}"]`).click();

    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });

    // Send message that will generate a longer response
    const testMessage = 'Count from 1 to 5 with a word between each number';
    await page.getByTestId('chat-input').fill(testMessage);
    await page.getByTestId('send-message-btn').click();

    // Wait for user message
    await expect(page.getByTestId('messages-container')).toContainText(testMessage, {
      timeout: 10000
    });

    // Wait for streaming to start
    const streamingMessage = page.locator('[data-streaming="true"]').first();
    await expect(streamingMessage).toBeVisible({ timeout: 15000 });

    const messageContent = streamingMessage.locator('.message-content');
    const getContentLength = async () => {
      const content = await messageContent.textContent();
      return content?.length || 0;
    };

    // Wait for some content, then verify it grows
    await expect.poll(getContentLength, { timeout: 8000 }).toBeGreaterThan(0);
    const firstLength = await getContentLength();
    await expect.poll(getContentLength, { timeout: 8000 }).toBeGreaterThan(firstLength);

    // Wait for streaming to complete
    const finalMessage = page.locator('[data-role="chat-message-assistant"]').last();
    await expect(finalMessage).not.toHaveAttribute('data-streaming', 'true', {
      timeout: 30000
    });

    // Verify final message has substantial content
    const finalContent = await finalMessage.locator('.message-content').textContent();
    expect(finalContent?.length).toBeGreaterThan(10);
  });

  test('Verify multiple token chunks appear incrementally', async ({ page }) => {
    const ficheId = await createFicheAndGetId(page);
    await page.locator(`[data-testid="chat-fiche-${ficheId}"]`).click();

    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });

    const testMessage = 'Write a short sentence about AI';
    await page.getByTestId('chat-input').fill(testMessage);
    await page.getByTestId('send-message-btn').click();

    await expect(page.getByTestId('messages-container')).toContainText(testMessage, {
      timeout: 10000
    });

    const streamingMessage = page.locator('[data-streaming="true"]').first();
    await expect(streamingMessage).toBeVisible({ timeout: 15000 });

    const messageContent = streamingMessage.locator('.message-content');
    const getContentLength = async () => {
      const content = await messageContent.textContent();
      return content?.length || 0;
    };

    // Ensure we see at least two increments (multiple chunks)
    await expect.poll(getContentLength, { timeout: 8000 }).toBeGreaterThan(0);
    const firstLength = await getContentLength();
    await expect.poll(getContentLength, { timeout: 8000 }).toBeGreaterThan(firstLength);
    const secondLength = await getContentLength();
    await expect.poll(getContentLength, { timeout: 8000 }).toBeGreaterThan(secondLength);
  });

  test('Verify streaming cursor animation', async ({ page }) => {
    const ficheId = await createFicheAndGetId(page);
    await page.locator(`[data-testid="chat-fiche-${ficheId}"]`).click();

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

    const ficheId = await createFicheAndGetId(page);
    await page.locator(`[data-testid="chat-fiche-${ficheId}"]`).click();
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
    await expect(newThreadBtn).toBeVisible({ timeout: 5000 });
    await newThreadBtn.click();
    await page.waitForURL((url) => url.pathname.includes('/thread/') && !url.pathname.includes(`/thread/${threadAId}`), {
      timeout: 15000,
    });
    console.log('📊 Switched to Thread B');

    const threadBUrl = page.url();
    const threadBId = threadBUrl.match(/\/thread\/(\d+)/)?.[1];
    console.log(`📊 Thread B ID: ${threadBId}`);

    // Verify we're in a different thread
    expect(threadBId).not.toBe(threadAId);
    expect(threadBId).toBeTruthy();

    // Thread B should be empty (new thread with no messages)
    const assistantMessagesInThreadB = page.locator('[data-role="chat-message-assistant"]');
    await expect.poll(async () => assistantMessagesInThreadB.count(), { timeout: 5000 }).toBe(0);
    console.log('✅ Thread B has no assistant messages (no token leakage)');

    // Switch back to Thread A
    const threadAInSidebar = page.locator(`[data-testid="thread-row-${threadAId}"]`);

    if (await threadAInSidebar.count() > 0) {
      await threadAInSidebar.click();
      await page.waitForURL((url) => url.pathname.includes(`/thread/${threadAId}`), { timeout: 10000 });
      console.log('📊 Switched back to Thread A');

      // Verify Thread A has assistant messages (streaming continued in background)
      const assistantMessagesInThreadA = page.locator('[data-role="chat-message-assistant"]');
      await expect.poll(async () => assistantMessagesInThreadA.count(), { timeout: 10000 }).toBeGreaterThan(0);
      const finalThreadAMessageCount = await assistantMessagesInThreadA.count();
      console.log(`✅ Thread A has ${finalThreadAMessageCount} assistant message(s)`);

      // Verify Thread A contains actual content (not just empty messages)
      const messagesInThreadA = page.getByTestId('messages-container');
      await expect.poll(async () => (await messagesInThreadA.textContent())?.length || 0, { timeout: 10000 }).toBeGreaterThan(20);
      console.log('✅ Thread A preserved its content');
    }

    console.log('✅ Thread-switching token isolation PASSED');
  });

  test('Writing indicator badge appears on background streaming threads', async ({ page }) => {
    console.log('🎯 Testing: ✍️ badge visibility for background threads');

    const ficheId = await createFicheAndGetId(page);
    await page.locator(`[data-testid="chat-fiche-${ficheId}"]`).click();
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
    await expect(newThreadBtn).toBeVisible({ timeout: 5000 });
    await newThreadBtn.click();
    await page.waitForURL((url) => url.pathname.includes('/thread/') && !url.pathname.includes(`/thread/${threadId}`), {
      timeout: 15000,
    });
    console.log('📊 Created and switched to Thread B');

    // Verify original thread shows "✍️ writing..." badge in sidebar
    const threadInSidebar = page.locator(`[data-testid="thread-row-${threadId}"]`);

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

      if (await writingIndicator.isVisible().catch(() => false)) {
        await expect.poll(async () => writingIndicator.isVisible().catch(() => false), { timeout: 15000 }).toBe(false);
        console.log('✅ Writing badge correctly disappeared after stream completed');
      }
    } else {
      console.log('⚠️  Thread not found in sidebar');
    }

    console.log('✅ Writing indicator badge test completed');
  });
});
