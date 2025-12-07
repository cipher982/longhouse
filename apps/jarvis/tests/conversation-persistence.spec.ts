import { test, expect } from '@playwright/test';

/**
 * Conversation persistence tests
 *
 * SKIP - These tests use window.addUserTurnToUI which no longer exists.
 * The tests need to be rewritten to use the new SessionManager API.
 */
test.describe.skip('Conversation Persistence', () => {
  test.beforeEach(async ({ page }) => {
    // Start with a clean state
    await page.goto('/');
    await page.waitForSelector('#transcript');

    // Wait for app initialization AND session to be active
    await page.waitForFunction(() => {
      const w = window as any;
      const sessionManager = w.stateManager?.getState?.()?.sessionManager;
      return sessionManager != null && sessionManager.isSessionActive?.() === true;
    }, { timeout: 30000 });
  });

  test('should persist messages within same conversation', async ({ page }) => {
    // Create new conversation
    await page.click('#newConversationBtn');
    await page.waitForTimeout(1000);

    // Mock a user message being added (simulate what happens during voice interaction)
    await page.evaluate(() => {
      // Simulate the addUserTurnToUI function call
      const transcript = document.querySelector('#transcript');
      if (transcript && window.addUserTurnToUI) {
        window.addUserTurnToUI('Hello, my name is David');
      }
    });

    // Verify user message appears
    await expect(page.locator('.user-turn')).toContainText('Hello, my name is David');

    // Mock assistant response
    await page.evaluate(() => {
      const transcript = document.querySelector('#transcript');
      if (transcript && window.addAssistantTurnToUI) {
        window.addAssistantTurnToUI('Nice to meet you, David! How can I help you today?');
      }
    });

    // Verify assistant message appears
    await expect(page.locator('.assistant-turn')).toContainText('Nice to meet you, David!');

    // Add another user message
    await page.evaluate(() => {
      if (window.addUserTurnToUI) {
        window.addUserTurnToUI('What tools do you have?');
      }
    });

    // Verify all messages are still visible
    const userTurns = page.locator('.user-turn');
    const assistantTurns = page.locator('.assistant-turn');

    await expect(userTurns).toHaveCount(2);
    await expect(assistantTurns).toHaveCount(1);

    await expect(userTurns.first()).toContainText('Hello, my name is David');
    await expect(userTurns.last()).toContainText('What tools do you have?');
    await expect(assistantTurns).toContainText('Nice to meet you, David!');
  });

  test('should persist messages across conversation switches', async ({ page }) => {
    // Create first conversation
    await page.click('#newConversationBtn');
    await page.waitForTimeout(1000);

    // Add messages to first conversation
    await page.evaluate(() => {
      if (window.addUserTurnToUI) {
        window.addUserTurnToUI('First conversation message');
      }
    });
    await page.evaluate(() => {
      if (window.addAssistantTurnToUI) {
        window.addAssistantTurnToUI('Response to first conversation');
      }
    });

    // Verify messages in first conversation
    await expect(page.locator('.user-turn')).toContainText('First conversation message');
    await expect(page.locator('.assistant-turn')).toContainText('Response to first conversation');

    // Create second conversation
    await page.click('#newConversationBtn');
    await page.waitForTimeout(1000);

    // Add messages to second conversation
    await page.evaluate(() => {
      if (window.addUserTurnToUI) {
        window.addUserTurnToUI('Second conversation message');
      }
    });
    await page.evaluate(() => {
      if (window.addAssistantTurnToUI) {
        window.addAssistantTurnToUI('Response to second conversation');
      }
    });

    // Verify only second conversation messages are visible
    await expect(page.locator('.user-turn')).toContainText('Second conversation message');
    await expect(page.locator('.assistant-turn')).toContainText('Response to second conversation');
    await expect(page.locator('.user-turn')).not.toContainText('First conversation message');

    // Get conversation items from sidebar
    const conversationItems = page.locator('.conversation-item:not(.empty)');
    await expect(conversationItems).toHaveCount(2);

    // Click on first conversation (should be the second item since they're sorted by update time)
    await conversationItems.nth(1).click();
    await page.waitForTimeout(1000);

    // Verify first conversation messages are restored
    await expect(page.locator('.user-turn')).toContainText('First conversation message');
    await expect(page.locator('.assistant-turn')).toContainText('Response to first conversation');

    // Verify second conversation messages are not visible
    await expect(page.locator('.user-turn')).not.toContainText('Second conversation message');
    await expect(page.locator('.assistant-turn')).not.toContainText('Response to second conversation');
  });

  test('should persist messages across page refresh', async ({ page }) => {
    // Create conversation and add messages
    await page.click('#newConversationBtn');
    await page.waitForTimeout(1000);

    await page.evaluate(() => {
      if (window.addUserTurnToUI) {
        window.addUserTurnToUI('Message before refresh');
      }
    });
    await page.evaluate(() => {
      if (window.addAssistantTurnToUI) {
        window.addAssistantTurnToUI('Assistant response before refresh');
      }
    });

    // Verify messages are visible
    await expect(page.locator('.user-turn')).toContainText('Message before refresh');
    await expect(page.locator('.assistant-turn')).toContainText('Assistant response before refresh');

    // Ensure IndexedDB writes are flushed before reload
    await page.evaluate(async () => {
      if (window.flushLocal) { await window.flushLocal(); }
    });
    await page.waitForTimeout(200);

    // Refresh the page
    await page.reload();
    await page.waitForSelector('#transcript');

    // Wait for app to reinitialize and load conversation history
    await page.waitForTimeout(3000);

    // Verify messages are restored after refresh
    await expect(page.locator('.user-turn')).toContainText('Message before refresh');
    await expect(page.locator('.assistant-turn')).toContainText('Assistant response before refresh');
  });

  test('should show conversation list with proper naming', async ({ page }) => {
    // Initially should show empty state
    await expect(page.locator('.conversation-item.empty')).toBeVisible();
    await expect(page.locator('.conversation-item.empty')).toContainText('No conversations yet');

    // Create first conversation
    await page.click('#newConversationBtn');
    await page.waitForTimeout(1000);

    // Should now show at least one conversation
    const conversationItems = page.locator('.conversation-item:not(.empty)');
    await expect(conversationItems).toHaveCount(1);

    // Should have meaningful name (not just timestamp)
    const firstConversation = conversationItems.first();
    const conversationName = await firstConversation.locator('.conversation-name').textContent();
    expect(conversationName).toMatch(/(Latest conversation|Chat from)/);

    // Create second conversation
    await page.click('#newConversationBtn');
    await page.waitForTimeout(1000);

    // Should now show two conversations
    await expect(conversationItems).toHaveCount(2);

    // First item should be "Latest conversation"
    await expect(conversationItems.first().locator('.conversation-name')).toContainText('Latest conversation');
  });

  test('should handle streaming messages correctly', async ({ page }) => {
    // Create conversation
    await page.click('#newConversationBtn');
    await page.waitForTimeout(1000);

    // Simulate streaming response (like what happens during real voice interaction)
    await page.evaluate(() => {
      // First add user message
      if (window.addUserTurnToUI) {
        window.addUserTurnToUI('Tell me about yourself');
      }

      // Then simulate streaming assistant response
      const transcript = document.querySelector('#transcript');
      if (transcript) {
        // Create streaming turn
        window.currentStreamingTurn = document.createElement('div');
        window.currentStreamingTurn.className = 'assistant-turn streaming';
        window.currentStreamingTurn.innerHTML = `
          <div class="turn-header"><svg class="role-icon assistant-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="11" r="1.5" fill="currentColor" stroke="none"/><circle cx="15" cy="11" r="1.5" fill="currentColor" stroke="none"/><path d="M9 16h6" stroke-width="2"/><path d="M8 1v3M16 1v3"/></svg> Assistant</div>
          <div class="streaming-text">I am an AI assistant...</div>
          <span class="cursor">|</span>
        `;
        transcript.appendChild(window.currentStreamingTurn);
      }
    });

    // Verify streaming message is visible
    await expect(page.locator('.assistant-turn.streaming')).toContainText('I am an AI assistant');
    await expect(page.locator('.cursor')).toBeVisible();

    // Add another user message while streaming (should insert before streaming)
    await page.evaluate(() => {
      if (window.addUserTurnToUI) {
        window.addUserTurnToUI('What can you do?');
      }
    });

    // Verify both user messages are visible and streaming continues
    const userTurns = page.locator('.user-turn');
    await expect(userTurns).toHaveCount(2);
    await expect(page.locator('.assistant-turn.streaming')).toContainText('I am an AI assistant');

    // Finalize streaming
    await page.evaluate(() => {
      if (window.currentStreamingTurn) {
        window.currentStreamingTurn.className = 'assistant-turn';
        const cursor = window.currentStreamingTurn.querySelector('.cursor');
        if (cursor) cursor.remove();
        window.currentStreamingTurn = null;
      }
    });

    // Verify streaming is finished and all messages persist
    await expect(page.locator('.assistant-turn:not(.streaming)')).toContainText('I am an AI assistant');
    await expect(page.locator('.cursor')).not.toBeVisible();
    await expect(userTurns).toHaveCount(2);
  });

  test('should maintain conversation state during context switching', async ({ page }) => {
    // Create conversation in current context (personal)
    await page.click('#newConversationBtn');
    await page.waitForTimeout(1000);

    await page.evaluate(() => {
      if (window.addUserTurnToUI) {
        window.addUserTurnToUI('Personal context message');
      }
    });
    // Flush writes to ensure persistence before context switch
    await page.evaluate(async () => {
      if (window.flushLocal) { await window.flushLocal(); }
    });
    await page.waitForTimeout(200);

    // Switch to work context (if available)
    const contextSelector = page.locator('#context-selector-container select');
    if (await contextSelector.count() > 0) {
      const options = await contextSelector.locator('option').count();
      if (options > 1) {
        await contextSelector.selectOption({ index: 1 });
        await page.waitForTimeout(2000);

        // Should show empty state in work context
        await expect(page.locator('.conversation-item.empty')).toBeVisible();

        // Switch back to personal context
        await contextSelector.selectOption({ index: 0 });
        await page.waitForTimeout(2000);

        // Should restore personal conversation
        await expect(page.locator('.user-turn')).toContainText('Personal context message');
      }
    }
  });
});
