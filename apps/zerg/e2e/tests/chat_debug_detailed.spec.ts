import { test, expect } from '@playwright/test';

test.describe('Detailed Chat Debug', () => {
  test('Debug chat flow with console logs', async ({ page }) => {
    // Capture all console messages
    const consoleMessages: string[] = [];
    page.on('console', msg => {
      consoleMessages.push(`${msg.type()}: ${msg.text()}`);
    });

    // Capture network requests
    const networkRequests: string[] = [];
    page.on('request', request => {
      networkRequests.push(`${request.method()} ${request.url()}`);
    });

    // Capture WebSocket frames
    const wsMessages: string[] = [];
    page.on('websocket', ws => {
      ws.on('framesent', event => {
        wsMessages.push(`SENT: ${event.payload}`);
      });
      ws.on('framereceived', event => {
        wsMessages.push(`RECEIVED: ${event.payload}`);
      });
    });

    console.log('🐛 Starting detailed debug test...');
    
    // Open the app
    await page.goto('http://localhost:8002');
    await page.waitForSelector('.dashboard-container', { timeout: 10000 });
    
    // Count agents before creation
    const initialCount = await page.locator('tr[data-agent-id]').count();
    console.log(`🐛 Initial agent count: ${initialCount}`);
    
    // Create agent
    await page.click('[data-testid="create-agent-btn"]');
    
    // Wait for the new agent to appear - give it some time for the async operation
    await page.waitForTimeout(3000);
    
    const finalCount = await page.locator('tr[data-agent-id]').count();
    console.log(`🐛 Final agent count: ${finalCount}`);
    
    // Get all agent IDs and find the new one
    const allAgentIds = await page.locator('tr[data-agent-id]').evaluateAll(rows => 
      rows.map(row => row.getAttribute('data-agent-id'))
    );
    console.log(`🐛 All visible agent IDs: ${JSON.stringify(allAgentIds)}`);
    
    // The newest agent should have the highest ID (since IDs are auto-increment)
    const agentIds = allAgentIds.map(id => parseInt(id || '0')).filter(id => id > 0);
    const newestAgentId = Math.max(...agentIds);
    console.log(`🐛 Using newest agent ID: ${newestAgentId}`);
    
    // Go to chat
    const chatButton = page.locator(`[data-testid="chat-agent-${newestAgentId}"]`);
    console.log(`🐛 Looking for chat button: [data-testid="chat-agent-${newestAgentId}"]`);
    await chatButton.click();
    await page.waitForSelector('.chat-input', { timeout: 10000 });
    
    // Send message
    const testMessage = 'Debug test message';
    await page.fill('.chat-input', testMessage);
    await page.click('[data-testid="send-message-btn"]');
    
    // Wait a bit for backend processing
    await page.waitForTimeout(3000);
    
    // Check what messages exist in DOM
    const allMessages = await page.locator('.message, .user-message, .assistant-message').count();
    const userMessages = await page.locator('.user-message').count();
    const assistantMessages = await page.locator('.assistant-message').count();
    
    console.log(`🐛 DOM Messages - Total: ${allMessages}, User: ${userMessages}, Assistant: ${assistantMessages}`);
    
    // Get all elements with potential message classes
    const messageElements = await page.locator('[class*="message"]').allTextContents();
    console.log(`🐛 Message elements: ${JSON.stringify(messageElements)}`);
    
    // Check chat state
    const messagesContainer = page.locator('.messages-container');
    const containerHTML = await messagesContainer.innerHTML();
    console.log(`🐛 Messages container HTML: ${containerHTML.substring(0, 500)}...`);
    
    // Output debugging info
    console.log('🐛 === CONSOLE MESSAGES ===');
    consoleMessages.forEach(msg => console.log(msg));
    
    console.log('🐛 === NETWORK REQUESTS ===');
    networkRequests.filter(req => req.includes('thread') || req.includes('message')).forEach(req => console.log(req));
    
    console.log('🐛 === WEBSOCKET MESSAGES ===');
    wsMessages.forEach(msg => console.log(msg));
    
    // Take screenshot for debugging
    await page.screenshot({ path: 'debug-chat-state.png' });
    
    console.log('🐛 Debug test completed - check output above');
  });
});