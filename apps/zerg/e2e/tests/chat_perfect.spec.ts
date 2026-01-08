import { test, expect } from './fixtures';

// Skip: Chat perfect test needs selector updates for new chat UI
test.skip();

// Reset DB before each test to keep agent/thread ids predictable
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
});

test.describe('Perfect Chat E2E Test', () => {
  test('Complete user flow: create agent → open chat → send message', async ({ page }) => {
    console.log('🧪 Starting perfect E2E chat test...');

    // Step 1: Open app
    console.log('📋 Step 1: Opening app...');
    await page.goto('/');

    // Wait for the page to fully load
    await expect(page.locator('[data-testid="create-agent-btn"]')).toBeVisible({ timeout: 15000 });
    console.log('✅ Dashboard loaded');

    // Verify we start with no agents
    const initialAgentRows = await page.locator('tr[data-agent-id]').count();
    console.log(`📋 Initial agent count: ${initialAgentRows}`);

    // Step 2: Click create agent
    console.log('📋 Step 2: Creating new agent...');
    await page.click('[data-testid="create-agent-btn"]');
    console.log('✅ Create agent button clicked');

    // Wait for agent to be created and appear in dashboard
    await page.waitForSelector('tr[data-agent-id]', { timeout: 15000 });
    console.log('✅ New agent appeared in dashboard');

    // Give the backend some time to create the default thread for the new agent
    await page.waitForTimeout(2000);

    // Get all agent IDs and find the newly created one (highest ID)
    const allAgentIds = await page.locator('tr[data-agent-id]').evaluateAll(rows =>
      rows.map(row => row.getAttribute('data-agent-id'))
    );
    const agentIds = allAgentIds.map(id => parseInt(id || '0')).filter(id => id > 0);
    const agentId = Math.max(...agentIds).toString();
    console.log(`📋 Agent created with ID: ${agentId}`);

    // Step 3: Click chat button on the new agent
    console.log('📋 Step 3: Clicking chat button...');
    const chatButton = page.locator(`[data-testid="chat-agent-${agentId}"]`);
    await expect(chatButton).toBeVisible({ timeout: 5000 });
    await chatButton.click();
    console.log('✅ Chat button clicked');

    // Wait for chat view to load
    await page.waitForSelector('[data-testid="chat-input"]', { timeout: 10000 });
    console.log('✅ Chat view loaded');

    // Verify chat UI elements are present
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible();
    await expect(page.locator('[data-testid="send-message-btn"]')).toBeVisible();
    await expect(page.locator('[data-testid="messages-container"]')).toBeVisible();
    console.log('✅ Chat UI elements verified');

    // Step 4: Send a message
    console.log('📋 Step 4: Sending message...');
    const testMessage = 'Hello! This is a perfect E2E test message.';

    // Fill in the message
    await page.fill('[data-testid="chat-input"]', testMessage);
    console.log(`📋 Message filled: "${testMessage}"`);

    // Click send button
    await page.click('[data-testid="send-message-btn"]');
    console.log('✅ Send button clicked');

    // Step 5: Verify the UI updates (deterministic; no LLM required)
    console.log('📋 Step 5: Verifying UI updates...');

    // Verify user message appears (optimistic update)
    await expect(page.locator('[data-testid="messages-container"]')).toContainText(testMessage, { timeout: 5000 });
    console.log('✅ User message appeared');

    // Verify input is cleared
    await expect(page.locator('[data-testid="chat-input"]')).toHaveValue('');
    console.log('✅ Input cleared after send');

    console.log('🎉 Perfect E2E test completed successfully!');
  });
});
