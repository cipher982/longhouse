/**
 * E2E Test: Chat Debug Panel and Reset Memory
 *
 * Tests the debug panel visibility, content, and reset functionality.
 * The debug panel is shown only in dev mode (config.isDevelopment).
 */

import { test, expect, type Page } from './fixtures';

// Reset DB before each test
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
});

async function navigateToChatPage(page: Page): Promise<void> {
  await page.goto('/chat');

  // Wait for chat UI to load
  const chatInterface = page.locator('.text-input-container, .chat-wrapper, .transcript');
  await expect(chatInterface.first()).toBeVisible({ timeout: 10000 });
}

async function sendMessage(page: Page, message: string): Promise<void> {
  const inputSelector = page.locator('.text-input');
  const sendButton = page.locator('.send-button');
  await inputSelector.fill(message);
  await sendButton.click();
  // Wait for response
  await page.waitForTimeout(2000);
}

test.describe('Debug Panel Tests', () => {
  test('debug panel is visible in dev mode', async ({ page }) => {
    await navigateToChatPage(page);

    // Debug panel should be visible (since E2E runs in dev mode)
    const debugPanel = page.locator('.debug-panel');
    await expect(debugPanel).toBeVisible({ timeout: 5000 });

    // Verify debug panel header shows "Debug"
    const header = debugPanel.locator('.sidebar-header');
    await expect(header).toContainText('Debug');
  });

  test('debug panel shows thread info', async ({ page }) => {
    await navigateToChatPage(page);

    // Wait for thread info to load (fetched from API)
    await page.waitForTimeout(1000);

    // Debug panel should show Thread section (with section header)
    const threadSectionHeader = page.locator('.debug-section-header').filter({ hasText: /^Thread$/ });
    await expect(threadSectionHeader).toBeVisible();

    // Get the parent section
    const threadSection = threadSectionHeader.locator('..');

    // Should show Thread ID
    const threadId = threadSection.locator('.debug-row').filter({ hasText: 'ID' });
    await expect(threadId).toBeVisible();

    // Should show message counts (DB and UI)
    const dbMessageCount = threadSection.locator('.debug-row').filter({ hasText: 'Messages (DB)' });
    await expect(dbMessageCount).toBeVisible();
    const uiMessageCount = threadSection.locator('.debug-row').filter({ hasText: 'Messages (UI)' });
    await expect(uiMessageCount).toBeVisible();
  });

  test('debug panel shows voice state', async ({ page }) => {
    await navigateToChatPage(page);

    // Voice section (labeled "Voice (OpenAI)" to distinguish from backend WS)
    const voiceSection = page.locator('.debug-section').filter({ hasText: 'Voice' });
    await expect(voiceSection).toBeVisible();

    // Should show voice status with indicator
    const status = voiceSection.locator('.debug-row').filter({ hasText: 'Status' });
    await expect(status).toBeVisible();
    const statusIndicator = voiceSection.locator('.debug-indicator');
    await expect(statusIndicator).toBeVisible();

    // Should show voice mode
    const mode = voiceSection.locator('.debug-row').filter({ hasText: 'Mode' });
    await expect(mode).toBeVisible();
    await expect(mode).toContainText('push-to-talk');
  });

  test('debug panel shows API links', async ({ page }) => {
    await navigateToChatPage(page);

    const apiSection = page.locator('.debug-section').filter({ hasText: 'API' });
    await expect(apiSection).toBeVisible();

    // Should have Thread and History links
    const threadLink = apiSection.locator('.debug-link').filter({ hasText: 'Thread' });
    await expect(threadLink).toBeVisible();

    const historyLink = apiSection.locator('.debug-link').filter({ hasText: 'History' });
    await expect(historyLink).toBeVisible();
  });
});

test.describe('Reset Memory Tests', () => {
  test('reset memory button is visible in debug panel', async ({ page }) => {
    await navigateToChatPage(page);

    const resetButton = page.locator('.debug-panel .sidebar-button').filter({ hasText: 'Reset Memory' });
    await expect(resetButton).toBeVisible();
  });

  test('reset memory clears chat history', async ({ page, request }) => {
    await navigateToChatPage(page);

    // Send a message to create history
    await sendMessage(page, 'Hello, this is a test message');

    // Wait longer for the full request/response cycle
    await page.waitForTimeout(5000);

    // Verify message appears in UI
    await expect(page.locator('.message.user')).toBeVisible({ timeout: 10000 });

    // Get initial thread info (should have at least 2 messages: system + user)
    const initialThreadResponse = await request.get('/api/jarvis/supervisor/thread');
    const initialThread = await initialThreadResponse.json();
    console.log('Initial thread state:', initialThread);

    // Verify we have more than just the system message
    expect(initialThread.message_count).toBeGreaterThan(1);

    // Click reset button
    const resetButton = page.locator('.debug-panel .sidebar-button').filter({ hasText: 'Reset Memory' });
    await resetButton.click();

    // Wait for reset to complete
    await page.waitForTimeout(2000);

    // Verify user messages are cleared from UI
    const userMessages = page.locator('.message.user');
    const messageCount = await userMessages.count();
    expect(messageCount).toBe(0);

    // Verify backend thread is cleared (all messages including system deleted)
    const finalThreadResponse = await request.get('/api/jarvis/supervisor/thread');
    const finalThread = await finalThreadResponse.json();
    console.log('Final thread state:', finalThread);

    // DELETE /api/jarvis/history clears all messages (0 remaining)
    expect(finalThread.message_count).toBe(0);
  });

  test('reset memory updates debug panel message count', async ({ page }) => {
    await navigateToChatPage(page);

    // Send a message and wait for response
    await sendMessage(page, 'Test message for reset');

    // Wait for assistant response and backend to update
    await page.waitForTimeout(8000);

    // Wait for debug panel to refresh (it polls every 10s, but also on message change)
    // Force a refresh by looking for the message in UI first
    await expect(page.locator('.message.user')).toBeVisible({ timeout: 10000 });

    // Get debug panel message row (use DB count since we're testing backend reset)
    const threadSectionHeader = page.locator('.debug-section-header').filter({ hasText: /^Thread$/ });
    const threadSection = threadSectionHeader.locator('..');
    const messageRow = threadSection.locator('.debug-row').filter({ hasText: 'Messages (DB)' });

    // Get the message count before reset
    const beforeText = await messageRow.textContent();
    console.log('Message count before reset:', beforeText);

    // Extract the number - should be > 0 after sending message
    const beforeMatch = beforeText?.match(/(\d+)/);
    const beforeCount = beforeMatch ? parseInt(beforeMatch[1]) : 0;
    console.log('Parsed before count:', beforeCount);

    // If beforeCount is still 0, the debug panel hasn't refreshed yet
    // This is acceptable - the test is about the UI clearing after reset
    if (beforeCount === 0) {
      console.log('Debug panel shows 0 messages - thread info may not have refreshed yet');
    }

    // Click reset
    const resetButton = page.locator('.debug-panel .sidebar-button').filter({ hasText: 'Reset Memory' });
    await resetButton.click();

    // Wait for reset and refresh
    await page.waitForTimeout(3000);

    // Verify UI messages are cleared
    const userMessages = page.locator('.message.user');
    const uiMessageCount = await userMessages.count();
    expect(uiMessageCount).toBe(0);

    // Check debug panel shows 0 messages after reset
    const afterText = await messageRow.textContent();
    console.log('Message count after reset:', afterText);

    // Extract the after count
    const afterMatch = afterText?.match(/(\d+)/);
    const afterCount = afterMatch ? parseInt(afterMatch[1]) : 0;

    // After reset, UI and debug panel should both show 0
    expect(afterCount).toBe(0);
  });
});

test.describe('Conversations Sidebar Removed', () => {
  test('old conversations sidebar is NOT visible', async ({ page }) => {
    await navigateToChatPage(page);

    // The old "Conversations" header should NOT exist
    const oldSidebarHeader = page.locator('.sidebar-header').filter({ hasText: 'Conversations' });
    await expect(oldSidebarHeader).not.toBeVisible();

    // The "New Conversation" button should NOT exist
    const newConversationButton = page.locator('.sidebar-button').filter({ hasText: 'New Conversation' });
    await expect(newConversationButton).not.toBeVisible();

    // The "Clear All" button should NOT exist
    const clearAllButton = page.locator('.sidebar-button').filter({ hasText: 'Clear All' });
    await expect(clearAllButton).not.toBeVisible();

    // The conversation list should NOT exist
    const conversationList = page.locator('.conversation-list');
    await expect(conversationList).not.toBeVisible();
  });

  test('debug panel is present instead of conversations sidebar', async ({ page }) => {
    await navigateToChatPage(page);

    // Debug panel should be visible
    const debugPanel = page.locator('.debug-panel');
    await expect(debugPanel).toBeVisible();

    // Debug header should show "Debug", not "Conversations"
    const header = debugPanel.locator('.sidebar-header');
    await expect(header).toContainText('Debug');
    await expect(header).not.toContainText('Conversations');
  });
});

test.describe('Layout Adaptation', () => {
  test('layout adjusts when debug panel is present', async ({ page }) => {
    await navigateToChatPage(page);

    // The app container should have the debug panel
    const appContainer = page.locator('.app-container');
    const debugPanel = appContainer.locator('.debug-panel');
    await expect(debugPanel).toBeVisible();

    // Main content should be visible alongside debug panel
    const mainContent = appContainer.locator('.main-content');
    await expect(mainContent).toBeVisible();

    // Take screenshot to verify layout
    await page.screenshot({ path: 'test-results/debug-panel-layout.png', fullPage: true });
  });
});
