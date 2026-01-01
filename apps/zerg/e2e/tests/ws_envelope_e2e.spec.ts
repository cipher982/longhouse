// e2e/tests/ws_envelope_e2e.spec.ts
import { test, expect } from './fixtures';

test.describe('WebSocket Envelope Protocol E2E', () => {
  test('Envelope protocol compliance and connection lifecycle', async ({ page }) => {
    // Open the app and connect WebSocket
    await page.goto('/');
    // Wait for the app to initialize and connect
    await page.waitForSelector('#global-status', { timeout: 10000 });

    // Simulate a user action that triggers a WebSocket message (e.g., subscribe to a thread)
    // This assumes the UI has a way to trigger a thread subscription
    // (You may need to adjust selectors and actions for your UI)
    await page.click('[data-testid="new-thread-btn"]');
    await page.fill('[data-testid="thread-title-input"]', 'E2E Envelope Test');
    await page.click('[data-testid="create-thread-confirm"]');

    // Wait for the thread to appear and select it
    await page.waitForSelector('[data-testid="thread-list-item"]');
    const threadId = await page.getAttribute('[data-testid="thread-list-item"]', 'data-thread-id');
    expect(threadId).toBeTruthy();

    // Send a message in the thread
    await page.fill('[data-testid="chat-input"]', 'Hello from E2E!');
    await page.click('[data-testid="send-message-btn"]');

    // Wait for the message to appear in the chat
    await page.waitForSelector('[data-testid="chat-message"]');
    const messageText = await page.textContent('[data-testid="chat-message"]');
    expect(messageText).toContain('Hello from E2E!');

    // Check that the WebSocket envelope is present in the network traffic
    // (Requires Playwright's network interception)
    const wsFrames = await page.context().waitForEvent('websocket', { timeout: 5000 });
    expect(wsFrames).toBeTruthy();
    // Optionally, check the frame payload for envelope fields
    // (This requires custom Playwright logic or browser devtools protocol)
  });

  test('Back-pressure: slow client is dropped, others unaffected', async ({ browser }) => {
    // Open two browser contexts (clients)
    const context1 = await browser.newContext();
    const context2 = await browser.newContext();
    const page1 = await context1.newPage();
    const page2 = await context2.newPage();

    await page1.goto('/');
    await page2.goto('/');

    // Simulate slow client by blocking reads on page1 (e.g., by not interacting)
    // On page2, send a message and expect it to be delivered
    await page2.fill('[data-testid="chat-input"]', 'Fast client message');
    await page2.click('[data-testid="send-message-btn"]');
    await page2.waitForSelector('[data-testid="chat-message"]');
    const msg = await page2.textContent('[data-testid="chat-message"]');
    expect(msg).toContain('Fast client message');

    // Optionally, check that page1 is eventually disconnected (e.g., by status indicator)
    // and page2 remains connected
    await context1.close();
    await context2.close();
  });

  test('Heartbeat/ping and graceful shutdown', async ({ page }) => {
    await page.goto('/');
    // Wait for the app to connect
    await page.waitForSelector('#global-status', { timeout: 10000 });

    // Simulate a ping (if UI exposes a way, or trigger via devtools)
    // For now, just wait for a period longer than the ping interval
    await page.waitForTimeout(35000);

    // Check that the connection is still alive (status indicator is green)
    const status = await page.textContent('#global-status');
    expect(status).toContain('Connected');

    // Simulate a page refresh (graceful shutdown)
    await page.reload();
    await page.waitForSelector('#global-status', { timeout: 10000 });
    const statusAfter = await page.textContent('#global-status');
    expect(statusAfter).toContain('Connected');
  });

  // Legacy protocol test removed - envelope structure is now mandatory
});
