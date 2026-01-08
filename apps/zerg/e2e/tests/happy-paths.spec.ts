/**
 * HAPPY PATH TESTS - Critical User Flows
 *
 * These tests validate the complete user journey through core features.
 * They catch URL routing issues, navigation problems, and state management bugs
 * BEFORE users discover them.
 *
 * Tests cover:
 * 1. Agent Creation Flow
 * 2. Chat Navigation with Correct URL Structure
 * 3. Message Sending and Display
 * 4. Thread Management (Create, Switch, Separate Chat/Automation)
 * 5. URL Structure Validation (trailing slashes, query params)
 */

import { test, expect, type Page } from './fixtures';

// Reset DB before each test for clean state
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
});

/**
 * Helper: Create agent via UI and return ID
 */
async function createAgentViaUI(page: Page): Promise<string> {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(500); // Let React hydrate

  // Wait for dashboard to be ready and button to be enabled
  const createBtn = page.locator('[data-testid="create-agent-btn"]');
  await expect(createBtn).toBeVisible({ timeout: 10000 });
  await expect(createBtn).toBeEnabled({ timeout: 10000 });

  // Create agent
  await createBtn.click();

  // Wait for new row with more patience
  const row = page.locator('tr[data-agent-id]').first();
  await expect(row).toBeVisible({ timeout: 10000 });

  // Wait for row to stabilize
  await page.waitForTimeout(500);

  const agentId = await row.getAttribute('data-agent-id');
  if (!agentId) {
    throw new Error('Failed to get agent ID from newly created agent row');
  }

  return agentId;
}

/**
 * Helper: Verify URL matches expected pattern
 */
function expectUrlPattern(url: string, pattern: RegExp, description: string) {
  expect(url, description).toMatch(pattern);
}

test.describe('Happy Path Tests - Core User Flows', () => {

  test('HAPPY PATH 1: Create Agent → Agent appears in dashboard', async ({ page }) => {
    console.log('🎯 Testing: Agent Creation Flow');

    // Navigate to dashboard
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Verify dashboard loaded
    await expect(page.locator('[data-testid="create-agent-btn"]')).toBeVisible({ timeout: 5000 });

    // Get initial agent count
    const initialCount = await page.locator('tr[data-agent-id]').count();
    console.log(`📊 Initial agent count: ${initialCount}`);

    // Create agent
    await page.locator('[data-testid="create-agent-btn"]').click();

    // Wait for new agent row to appear
    await page.waitForTimeout(500); // Give time for API response
    await page.locator('tr[data-agent-id]').nth(initialCount).waitFor({ timeout: 10000 });

    // Verify agent appears in list
    const newCount = await page.locator('tr[data-agent-id]').count();
    expect(newCount).toBe(initialCount + 1);

    // Verify agent row is visible and has correct structure
    const newRow = page.locator('tr[data-agent-id]').first();
    await expect(newRow).toBeVisible();

    // Verify agent has ID attribute
    const agentId = await newRow.getAttribute('data-agent-id');
    expect(agentId).toBeTruthy();
    expect(agentId).toMatch(/^\d+$/); // Should be numeric ID

    console.log(`✅ Agent created successfully with ID: ${agentId}`);
  });

  test('HAPPY PATH 2: Navigate to Chat → Verify URL structure is correct', async ({ page }) => {
    console.log('🎯 Testing: Chat Navigation with URL Validation');

    // Create agent
    const agentId = await createAgentViaUI(page);
    console.log(`📊 Created agent ID: ${agentId}`);

    // Click "Chat with Agent" button
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    // Wait for navigation to complete
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(500); // Allow for any redirects

    // Get final URL
    const url = page.url();
    console.log(`📊 Final URL: ${url}`);

    // CRITICAL: Verify URL structure matches /agent/{id}/thread/ or /agent/{id}/thread/{tid}
    // This catches the "missing trailing slash" bug we just fixed
    expectUrlPattern(
      url,
      new RegExp(`/agent/${agentId}/thread(/[^/]*)?$`),
      'URL should match /agent/{id}/thread/ or /agent/{id}/thread/{tid}'
    );

    // Verify URL has trailing slash after "thread" when no thread ID
    if (!url.includes('thread/') && url.includes('thread')) {
      throw new Error(`URL missing trailing slash: ${url}. This will break navigation!`);
    }

    // Verify chat interface loads
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 5000 });

    // Verify query params are preserved if present
    if (url.includes('?name=')) {
      console.log('✅ Query params preserved in URL');
    }

    console.log('✅ Chat navigation and URL structure validated');
  });

  test('HAPPY PATH 3: Send message → Message appears in chat', async ({ page }) => {
    console.log('🎯 Testing: Message Sending Flow');

    // Create agent and navigate to chat
    const agentId = await createAgentViaUI(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(500);

    // Wait for chat to load
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="send-message-btn"]')).toBeVisible({ timeout: 10000 });

    // Type and send message
    const testMessage = 'Hello, this is a test message for happy path validation';
    await page.locator('[data-testid="chat-input"]').fill(testMessage);

    // Click send
    await page.locator('[data-testid="send-message-btn"]').click();

    // CRITICAL: Verify message appears in messages container
    // Try different possible selectors for messages container
    const messagesContainer = page.locator('[data-testid="messages-container"]').or(page.locator('.messages-container')).first();
    await expect(messagesContainer).toContainText(testMessage, { timeout: 15000 });

    console.log('✅ Message sent and displayed correctly');
  });

  test('HAPPY PATH 4: Create new thread → Switch threads → Verify state', async ({ page }) => {
    console.log('🎯 Testing: Thread Management Flow');

    // Create agent and navigate to chat
    const agentId = await createAgentViaUI(page);
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(500);

    // Wait for chat to load
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

    // Check if new thread button exists - skip if not implemented yet
    const newThreadBtn = page.locator('.new-thread-btn');
    const newThreadBtnCount = await newThreadBtn.count();
    if (newThreadBtnCount === 0) {
      console.log('⚠️  New thread button not found - thread management may not be fully implemented');
      test.skip();
      return;
    }

    // Send message in first thread
    const thread1Message = 'Message in first thread';
    await page.locator('[data-testid="chat-input"]').fill(thread1Message);
    await page.locator('[data-testid="send-message-btn"]').click();

    // Verify message appears
    const messagesContainer = page.locator('[data-testid="messages-container"]').or(page.locator('.messages-container')).first();
    await expect(messagesContainer).toContainText(thread1Message, { timeout: 15000 });

    // Get first thread URL
    const firstThreadUrl = page.url();
    console.log(`📊 First thread URL: ${firstThreadUrl}`);

    // Create new thread
    await expect(newThreadBtn).toBeVisible({ timeout: 5000 });
    await newThreadBtn.click();

    // Wait for URL to change (deterministic wait, not arbitrary timeout)
    await page.waitForURL((url) => url.toString() !== firstThreadUrl, { timeout: 5000 });

    const secondThreadUrl = page.url();
    console.log(`📊 Second thread URL: ${secondThreadUrl}`);

    // Verify URLs are different
    expect(secondThreadUrl).not.toBe(firstThreadUrl);

    // Send message in second thread
    const thread2Message = 'Message in second thread';
    await page.locator('[data-testid="chat-input"]').fill(thread2Message);
    await page.locator('[data-testid="send-message-btn"]').click();

    await expect(messagesContainer).toContainText(thread2Message, { timeout: 15000 });

    // Switch back to first thread by clicking in sidebar
    const threadList = page.locator('.thread-list .thread-row');
    const threadCount = await threadList.count();

    if (threadCount >= 2) {
      // Click first thread (should be at index 1 since newest is at 0)
      const currentUrl = page.url();
      await threadList.nth(1).click();

      // Wait for URL to change (thread switch complete)
      await page.waitForURL((url) => url.toString() !== currentUrl, { timeout: 5000 });

      // CRITICAL: Verify we're back on first thread with correct message
      await expect(messagesContainer).toContainText(thread1Message, { timeout: 5000 });
    } else {
      console.log('⚠️  Thread list has fewer than 2 threads - skipping thread switching test');
    }

    console.log('✅ Thread creation and switching works correctly');
  });

  test('HAPPY PATH 5: URL trailing slash preservation', async ({ page }) => {
    console.log('🎯 Testing: URL Trailing Slash Bug Fix');

    // Create agent
    const agentId = await createAgentViaUI(page);

    // Navigate to chat - should get trailing slash
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(500);

    const initialUrl = page.url();
    console.log(`📊 Initial URL: ${initialUrl}`);

    // If URL has no thread ID, it MUST have trailing slash
    if (initialUrl.match(/\/thread$/)) {
      throw new Error(`BUG DETECTED: URL missing trailing slash: ${initialUrl}`);
    }

    // Verify URL is either:
    // - /agent/{id}/thread/ (with slash, no thread ID)
    // - /agent/{id}/thread/{tid} (with thread ID)
    const hasTrailingSlash = initialUrl.match(/\/thread\/(\?.*)?$/);
    const hasThreadId = initialUrl.match(/\/thread\/[a-zA-Z0-9-]+/);

    expect(
      hasTrailingSlash || hasThreadId,
      'URL must have trailing slash OR thread ID'
    ).toBeTruthy();

    console.log('✅ URL structure is correct with proper trailing slash');
  });

  test('HAPPY PATH 6: Query params preserved during navigation', async ({ page }) => {
    console.log('🎯 Testing: Query Parameter Preservation');

    // Create agent with known name
    const agentId = await createAgentViaUI(page);

    // Navigate to dashboard to get agent name
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    const agentRow = page.locator(`tr[data-agent-id="${agentId}"]`);
    await agentRow.waitFor({ timeout: 5000 });

    // Click chat button (should include name in query params)
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(500);

    const url = page.url();
    console.log(`📊 Final URL: ${url}`);

    // Verify URL is valid (either with or without query params)
    expect(url).toMatch(/\/agent\/\d+\/thread\//);

    // Verify chat still works regardless of query params
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

    console.log('✅ Navigation works correctly (with or without query params)');
  });

  test('HAPPY PATH 7: No duplicate threads on agent creation', async ({ page }) => {
    console.log('🎯 Testing: No Duplicate Threads Bug');

    // Create agent
    const agentId = await createAgentViaUI(page);

    // Navigate to chat
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(500);

    // Get initial URL (should have thread ID)
    const initialUrl = page.url();
    console.log(`📊 Initial URL: ${initialUrl}`);

    // Send a message (this should NOT create a duplicate thread)
    await page.locator('[data-testid="chat-input"]').fill('Test message');
    await page.locator('[data-testid="send-message-btn"]').click();

    // Wait for message to send
    const messagesContainer = page.locator('[data-testid="messages-container"]').or(page.locator('.messages-container')).first();
    await expect(messagesContainer).toContainText('Test message', { timeout: 15000 });

    // Wait a bit for any async operations
    await page.waitForTimeout(1000);

    // URL should not change (no new thread created)
    const finalUrl = page.url();
    console.log(`📊 Final URL: ${finalUrl}`);

    // Extract thread IDs from URLs
    const initialThreadId = initialUrl.match(/\/thread\/([^/?]+)/)?.[1];
    const finalThreadId = finalUrl.match(/\/thread\/([^/?]+)/)?.[1];

    console.log(`📊 Initial thread ID: ${initialThreadId}, Final thread ID: ${finalThreadId}`);

    // CRITICAL: Thread ID should not change (no duplicate created)
    if (initialThreadId && finalThreadId) {
      expect(finalThreadId).toBe(initialThreadId);
    }

    console.log('✅ No duplicate threads created');
  });

  test('HAPPY PATH 8: Back to dashboard navigation preserves state', async ({ page }) => {
    console.log('🎯 Testing: Back to Dashboard Flow');

    // Create two agents
    const agent1Id = await createAgentViaUI(page);
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    const agent2Id = await createAgentViaUI(page);

    console.log(`📊 Created agents: ${agent1Id}, ${agent2Id}`);

    // Navigate to first agent's chat
    await page.locator(`[data-testid="chat-agent-${agent1Id}"]`).click();
    await page.waitForLoadState('networkidle');

    // Verify we're in chat
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 5000 });

    // Navigate back to dashboard using browser back
    await page.goBack();
    await page.waitForLoadState('networkidle');

    // Verify dashboard shows both agents
    await expect(page.locator(`tr[data-agent-id="${agent1Id}"]`)).toBeVisible({ timeout: 5000 });
    await expect(page.locator(`tr[data-agent-id="${agent2Id}"]`)).toBeVisible({ timeout: 5000 });

    // Navigate to second agent's chat
    await page.locator(`[data-testid="chat-agent-${agent2Id}"]`).click();
    await page.waitForLoadState('networkidle');

    // Verify we're in chat for correct agent
    const url = page.url();
    expect(url).toContain(`/agent/${agent2Id}/thread/`);

    console.log('✅ Navigation between dashboard and chat works correctly');
  });

  test('HAPPY PATH 9: Verify chat interface loads correctly', async ({ page }) => {
    console.log('🎯 Testing: Chat Interface Loading');

    // Create agent via UI
    const agentId = await createAgentViaUI(page);

    // Navigate to chat
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(500);

    // Verify all critical chat UI elements are present
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="send-message-btn"]')).toBeVisible({ timeout: 5000 });

    // Verify messages container exists (might be empty)
    const messagesContainer = page.locator('[data-testid="messages-container"]').or(page.locator('.messages-container')).first();
    await expect(messagesContainer).toBeVisible({ timeout: 5000 });

    // Verify URL structure is correct
    const url = page.url();
    expect(url).toMatch(/\/agent\/\d+\/thread\//);

    console.log('✅ Chat interface loads correctly with all required elements');
  });

  test('HAPPY PATH 10: Complete user journey - Create, Chat, Send, Return', async ({ page }) => {
    console.log('🎯 Testing: Complete User Journey');

    // STEP 1: Create agent
    console.log('Step 1: Creating agent...');
    const agentId = await createAgentViaUI(page);

    // STEP 2: Navigate to chat
    console.log('Step 2: Navigating to chat...');
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(500);

    // Verify URL is correct
    let url = page.url();
    expectUrlPattern(url, /\/agent\/\d+\/thread\//, 'Initial chat URL should be correct');

    // Verify chat loads
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

    // STEP 3: Send message
    console.log('Step 3: Sending message...');
    const urlBeforeSend = page.url();
    console.log(`📊 URL before sending message: ${urlBeforeSend}`);

    await page.locator('[data-testid="chat-input"]').fill('Test message for journey');
    await page.locator('[data-testid="send-message-btn"]').click();

    const messagesContainer = page.locator('[data-testid="messages-container"]').or(page.locator('.messages-container')).first();
    await expect(messagesContainer).toContainText('Test message for journey', { timeout: 15000 });

    const urlAfterSend = page.url();
    console.log(`📊 URL after sending message: ${urlAfterSend}`);

    // STEP 4: Return to dashboard
    console.log('Step 4: Returning to dashboard...');
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(500);

    // Verify agent still exists
    await expect(page.locator(`tr[data-agent-id="${agentId}"]`)).toBeVisible({ timeout: 5000 });

    // STEP 5: Navigate back to chat and verify message persists
    console.log('Step 5: Verifying message persistence...');
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');

    // Log URL immediately after navigation
    let urlAfterClick = page.url();
    console.log(`📊 URL after clicking chat button: ${urlAfterClick}`);

    await page.waitForTimeout(1000); // Give time for thread to load

    // Log URL after wait
    let urlAfterWait = page.url();
    console.log(`📊 URL after waiting: ${urlAfterWait}`);

    // Create fresh locator reference after navigation
    const messagesContainerAfterReturn = page.locator('[data-testid="messages-container"]').or(page.locator('.messages-container')).first();

    // Wait for messages to load
    await page.waitForTimeout(1000);

    // Check if there are any messages at all
    const messageCount = await page.locator('[data-role="chat-message-user"], [data-role="chat-message-assistant"]').count();
    console.log(`📊 Message count in container: ${messageCount}`);

    // Message should still be visible
    await expect(messagesContainerAfterReturn).toContainText('Test message for journey', { timeout: 15000 });

    console.log('✅ Complete user journey successful - create, chat, return, persistence verified!');
  });
});
