import { test, expect, type Page } from './fixtures';
import { WebSocketServer, WebSocket } from 'ws';

// Reset DB before each test to keep thread ids predictable
test.beforeEach(async ({ request }) => {
  await request.post('http://localhost:8001/admin/reset-database');
});

async function createAgentAndGetId(page: Page): Promise<string> {
  await page.goto('/');
  await page.locator('[data-testid="create-agent-btn"]').click();
  const row = page.locator('tr[data-agent-id]').first();
  await expect(row).toBeVisible();
  return (await row.getAttribute('data-agent-id')) as string;
}

test.describe('Agent Response E2E', () => {
  // SKIPPED: This test uses custom WebSocket mocking and expects CSS classes
  // (.chat-input, .send-button, .messages-container) that don't exist in the
  // current ChatPage implementation. Requires significant rework.
  test.skip('Agent response should appear in chat without reload', async ({ page }) => {
    // --- Mock WebSocket Server ---
    const wss = new WebSocketServer({ port: 8080 });
    let clientWs: WebSocket | null = null;

    wss.on('connection', ws => {
      clientWs = ws;
      ws.on('message', message => {
        // For debugging: log messages from the client
        console.log('received: %s', message);
      });
    });

    // --- Intercept WebSocket creation in the browser ---
    await page.addInitScript(() => {
      const OriginalWebSocket = window.WebSocket;
      window.WebSocket = function(url, protocols) {
        if (url.includes('/api/ws')) {
          console.log('Redirecting WebSocket connection to mock server');
          return new OriginalWebSocket('ws://localhost:8080', protocols);
        }
        return new OriginalWebSocket(url, protocols);
      } as any;
    });

    const agentId = await createAgentAndGetId(page);

    // Enter chat view
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForSelector('.chat-input', { state: 'visible' });

    // Send a message
    const chatInput = page.locator('.chat-input');
    await chatInput.fill('Hello, agent!');
    await page.locator('.send-button').click();

    // Expect the user's message to appear
    await expect(page.locator('.messages-container')).toContainText('Hello, agent!');

    // Wait for the client to connect to our mock server
    await new Promise<void>(resolve => {
        const interval = setInterval(() => {
            if (clientWs) {
                clearInterval(interval);
                resolve();
            }
        }, 100);
    });

    // Mock the agent's response
    const mockResponse = {
        topic: 'thread:1',
        data: {
            type: 'thread.message',
            data: {
                id: 'msg_abc123',
                thread_id: 1,
                role: 'assistant',
                content: [{ type: 'text', text: { value: 'This is a mocked agent response.' } }],
                created_at: new Date().toISOString(),
            }
        }
    };

    // Send the mock response to the client
    clientWs!.send(JSON.stringify(mockResponse));

    // Assert that the mocked response is visible
    await expect(page.locator('.messages-container')).toContainText('This is a mocked agent response.', { timeout: 5000 });

    // --- Cleanup ---
    wss.close();
  });
});
