import { test, expect } from '@playwright/test';

test.describe('Perfect Chat E2E Test', () => {
  test('Complete user flow: blank slate → create agent → chat → send message', async ({ page }) => {
    console.log('🧪 Starting perfect E2E chat test...');

    // Step 1: Open blank slate website
    console.log('📋 Step 1: Opening blank slate website...');
    await page.goto('http://localhost:8002');

    // Wait for the page to fully load
    await page.waitForSelector('.dashboard-container', { timeout: 10000 });
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
    await page.waitForSelector('.chat-input', { timeout: 10000 });
    console.log('✅ Chat view loaded');

    // Verify chat UI elements are present
    await expect(page.locator('.chat-input')).toBeVisible();
    await expect(page.locator('[data-testid="send-message-btn"]')).toBeVisible();
    await expect(page.locator('.messages-container')).toBeVisible();
    console.log('✅ Chat UI elements verified');

    // Step 4: Send a message
    console.log('📋 Step 4: Sending message...');
    const testMessage = 'Hello! This is a perfect E2E test message.';

    // Fill in the message
    await page.fill('.chat-input', testMessage);
    console.log(`📋 Message filled: "${testMessage}"`);

    // Click send button
    await page.click('[data-testid="send-message-btn"]');
    console.log('✅ Send button clicked');

    // Step 5: Verify the complete conversation flow
    console.log('📋 Step 5: Verifying conversation flow...');

    // 5a: Verify user message appears immediately (optimistic update)
    await expect(page.locator('.user-message').filter({ hasText: testMessage })).toBeVisible({ timeout: 5000 });
    console.log('✅ User message appeared (optimistic update)');

    // 5b: Verify input is cleared
    await expect(page.locator('.chat-input')).toHaveValue('');
    console.log('✅ Input cleared after send');

    // 5c: Wait for and verify assistant response appears (streaming)
    await expect(page.locator('.assistant-message')).toBeVisible({ timeout: 15000 });
    console.log('✅ Assistant response appeared');

    // 5d: Verify assistant response has content
    const assistantMessage = page.locator('.assistant-message').first();
    const responseText = await assistantMessage.textContent();
    expect(responseText?.length).toBeGreaterThan(0);
    console.log(`✅ Assistant response content: "${responseText?.substring(0, 50)}..."`);

    // 5e: Verify conversation structure
    const userMessages = await page.locator('.user-message').count();
    const assistantMessages = await page.locator('.assistant-message').count();
    expect(userMessages).toBe(1);
    expect(assistantMessages).toBe(1);
    console.log(`✅ Conversation structure verified: ${userMessages} user, ${assistantMessages} assistant`);

    // Optional: Test sending a second message to verify continued conversation
    console.log('📋 Step 6: Testing second message...');
    const secondMessage = 'Can you confirm you received my first message?';
    await page.fill('.chat-input', secondMessage);
    await page.click('[data-testid="send-message-btn"]');

    // Wait for second user message
    await expect(page.locator('.user-message').nth(1)).toBeVisible({ timeout: 5000 });
    console.log('✅ Second user message appeared');

    // Wait for second assistant response
    await expect(page.locator('.assistant-message').nth(1)).toBeVisible({ timeout: 15000 });
    console.log('✅ Second assistant response appeared');

    // Final verification: Should have 2 user messages and 2 assistant messages
    const finalUserCount = await page.locator('.user-message').count();
    const finalAssistantCount = await page.locator('.assistant-message').count();
    expect(finalUserCount).toBe(2);
    expect(finalAssistantCount).toBe(2);

    console.log('🎉 Perfect E2E test completed successfully!');
    console.log(`📊 Final conversation: ${finalUserCount} user messages, ${finalAssistantCount} assistant responses`);
  });
});
