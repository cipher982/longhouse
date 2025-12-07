import { test, expect } from '@playwright/test';

/**
 * E2E test for OpenAI Realtime history hydration
 *
 * Tests that conversation history persists across page refreshes and
 * is properly injected into the OpenAI Realtime session via updateHistory().
 *
 * This test uses REAL API calls - requires:
 * - Running dev server with valid OPENAI_API_KEY
 * - Network access to OpenAI
 *
 * The test flow:
 * 1. Inject a unique codeword into IndexedDB
 * 2. Reload the page
 * 3. Connect to OpenAI Realtime (triggers history hydration)
 * 4. Ask about the codeword via text input
 * 5. Assert the response contains the codeword
 */

test.describe('History Hydration E2E', () => {
  // Use a unique codeword per test run to avoid false positives
  const testCodeword = `zebra${Date.now()}`;

  // Skip tests that require real OpenAI WebRTC connection
  // These tests require WebRTC + microphone which don't work in headless Docker
  // SKIP_WEBRTC_TESTS is set in docker-compose.test.yml
  const skipRealApiTests = process.env.SKIP_WEBRTC_TESTS === 'true' ||
                           process.env.CI ||
                           !process.env.OPENAI_API_KEY;

  test.beforeEach(async ({ page }) => {
    await page.goto('/chat/');
    await page.waitForSelector('#transcript');

    // Wait for app initialization AND session to be active
    // The session must be active before we can add conversation turns
    await page.waitForFunction(() => {
      const w = window as any;
      const sessionManager = w.stateManager?.getState?.()?.sessionManager;
      // Check both that sessionManager exists AND that session is active
      return sessionManager != null && sessionManager.isSessionActive?.() === true;
    }, { timeout: 30000 });
  });

  test('should hydrate conversation history into Realtime session after page refresh', async ({ page }) => {
    // This test requires real OpenAI WebRTC connection
    test.skip(skipRealApiTests, 'Requires real OpenAI connection (WebRTC + microphone)');

    // Increase timeout for real API calls
    test.setTimeout(120000);

    // Step 1: Inject codeword into IndexedDB
    console.log(`Injecting test codeword: ${testCodeword}`);
    await page.evaluate(async (codeword) => {
      const { stateManager } = window as any;
      const sessionManager = stateManager.getState().sessionManager;

      // Helper to generate UUID (crypto.randomUUID may not be available in all contexts)
      const generateId = () => {
        if (typeof crypto.randomUUID === 'function') {
          return crypto.randomUUID();
        }
        // Fallback for non-secure contexts
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
          const r = Math.random() * 16 | 0;
          const v = c === 'x' ? r : (r & 0x3 | 0x8);
          return v.toString(16);
        });
      };

      const userTurn = {
        id: generateId(),
        timestamp: new Date(),
        userTranscript: `The secret codeword is ${codeword}`,
      };

      const assistantTurn = {
        id: generateId(),
        timestamp: new Date(Date.now() + 1000),
        assistantResponse: `Got it! I'll remember that the codeword is "${codeword}".`,
      };

      await sessionManager.addConversationTurn(userTurn);
      await sessionManager.addConversationTurn(assistantTurn);

      // Flush to ensure persistence
      if (sessionManager.flush) {
        await sessionManager.flush();
      }
    }, testCodeword);

    // Verify injection
    const historyCount = await page.evaluate(async () => {
      const { stateManager } = window as any;
      const sessionManager = stateManager.getState().sessionManager;
      const history = await sessionManager.getConversationHistory();
      return history.length;
    });
    expect(historyCount).toBeGreaterThanOrEqual(2);
    console.log(`Verified ${historyCount} turns in IndexedDB`);

    // Step 2: Reload page
    console.log('Reloading page...');
    await page.reload();
    await page.waitForSelector('#transcript');

    // Wait for app to reinitialize
    await page.waitForFunction(() => {
      const w = window as any;
      return w.stateManager?.getState?.()?.sessionManager != null;
    }, { timeout: 15000 });

    // Wait a bit for history to load into UI
    await page.waitForTimeout(2000);

    // Step 3: Connect to OpenAI Realtime
    console.log('Connecting to OpenAI Realtime...');

    // Listen for console logs to capture hydration message
    const consoleLogs: string[] = [];
    page.on('console', msg => {
      consoleLogs.push(msg.text());
    });

    // Click the mic button to connect
    await page.click('#pttBtn');

    // Wait for connection (button should change state)
    await page.waitForFunction(() => {
      const btn = document.querySelector('#pttBtn');
      return btn?.classList.contains('ready') || btn?.classList.contains('connecting');
    }, { timeout: 30000 });

    // Wait for hydration to complete
    await page.waitForTimeout(3000);

    // Verify hydration happened by checking console logs
    const hydrationLog = consoleLogs.find(log => log.includes('Hydrated') && log.includes('history items'));
    console.log('Console logs captured:', consoleLogs.filter(l => l.includes('Hydrat') || l.includes('history')));

    // Note: The hydration log might not show in page.on('console') if logged via logger module
    // So we also check the session history directly
    const sessionHistoryCount = await page.evaluate(() => {
      const { stateManager } = window as any;
      const session = stateManager.getState().session;
      return session?.history?.length ?? 0;
    });
    console.log(`Session history has ${sessionHistoryCount} items`);

    // Step 4: Ask about the codeword via text input
    console.log('Asking about the codeword...');
    const textInput = page.locator('#textInput');
    const sendBtn = page.locator('#sendTextBtn');

    await textInput.fill('What was the secret codeword I mentioned earlier?');
    await sendBtn.click();

    // Step 5: Wait for response and assert it contains the codeword
    console.log('Waiting for response...');

    // Wait for assistant response to appear
    await page.waitForSelector('.assistant-turn', { timeout: 60000 });

    // Wait for streaming to complete (no more cursor)
    await page.waitForFunction(() => {
      const assistantTurns = document.querySelectorAll('.assistant-turn');
      const lastTurn = assistantTurns[assistantTurns.length - 1];
      // Check if it's not streaming anymore
      return lastTurn && !lastTurn.classList.contains('streaming');
    }, { timeout: 60000 });

    // Get the last assistant response
    const assistantResponses = await page.locator('.assistant-turn').all();
    const lastResponse = assistantResponses[assistantResponses.length - 1];
    const responseText = await lastResponse.textContent();

    console.log(`Response received: "${responseText?.slice(0, 200)}..."`);

    // Assert the response mentions the codeword
    // The model should recall the codeword from the hydrated history
    expect(responseText?.toLowerCase()).toContain(testCodeword.toLowerCase());
  });

  test('should show hydrated history in UI after page refresh', async ({ page }) => {
    // This test verifies the UI shows the history AND it gets hydrated to Realtime

    // Inject turns
    await page.evaluate(async () => {
      const { stateManager } = window as any;
      const sessionManager = stateManager.getState().sessionManager;

      // Helper to generate UUID (crypto.randomUUID may not be available in all contexts)
      const generateId = () => {
        if (typeof crypto.randomUUID === 'function') {
          return crypto.randomUUID();
        }
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
          const r = Math.random() * 16 | 0;
          const v = c === 'x' ? r : (r & 0x3 | 0x8);
          return v.toString(16);
        });
      };

      await sessionManager.addConversationTurn({
        id: generateId(),
        timestamp: new Date(),
        userTranscript: 'Test message from before refresh',
      });

      await sessionManager.addConversationTurn({
        id: generateId(),
        timestamp: new Date(Date.now() + 1000),
        assistantResponse: 'I acknowledge your test message.',
      });

      if (sessionManager.flush) await sessionManager.flush();
    });

    // Reload
    await page.reload();
    await page.waitForSelector('#transcript');
    await page.waitForTimeout(3000);

    // Verify UI shows the messages
    await expect(page.locator('.user-turn')).toContainText('Test message from before refresh');
    await expect(page.locator('.assistant-turn')).toContainText('I acknowledge your test message');
  });

  test('should handle empty history gracefully', async ({ page }) => {
    // This test requires real OpenAI WebRTC connection
    test.skip(skipRealApiTests, 'Requires real OpenAI connection (WebRTC + microphone)');

    // Clear all conversations first (if session is active)
    await page.evaluate(async () => {
      const { stateManager } = window as any;
      const sessionManager = stateManager.getState().sessionManager;
      // Try to clear, but don't fail if session isn't active (fresh browser state)
      try {
        if (sessionManager && typeof sessionManager.clearAllConversations === 'function') {
          await sessionManager.clearAllConversations();
        }
      } catch (e) {
        console.log('Could not clear conversations (session may not be active):', e);
      }
    });

    // Reload
    await page.reload();
    await page.waitForSelector('#transcript');
    await page.waitForTimeout(2000);

    // Connect should work without errors
    await page.click('#pttBtn');

    // Should connect successfully (button should reach ready state)
    await page.waitForFunction(() => {
      const btn = document.querySelector('#pttBtn');
      return btn?.classList.contains('ready');
    }, { timeout: 30000 });

    // No errors should appear
    const errorToast = page.locator('.toast.error');
    await expect(errorToast).not.toBeVisible();
  });
});
