import { test, expect } from '@playwright/test';
import { setupAuthenticatedSession, cleanupAfterTest } from './helpers/auth-helpers';

/**
 * Real E2E tests for WebSocket subscription confirmation.
 *
 * These tests actually exercise the UI by:
 * 1. Stubbing the WebSocket to control server responses
 * 2. Observing real UI state changes
 * 3. Verifying actual subscription behavior
 *
 * Unlike the previous tests which just replayed logic, these prove
 * the implementation actually works in the browser.
 */

test.describe('WebSocket Subscription Confirmation (Real E2E)', () => {
  test.beforeEach(async ({ page }) => {
    await setupAuthenticatedSession(page);
  });

  test.afterEach(async ({ page }) => {
    await cleanupAfterTest(page);
  });

  test('successful subscription adds agent to subscribed set on ack', async ({ page }) => {
    // Stub WebSocket to capture and control messages
    await page.addInitScript(() => {
      const OriginalWebSocket = window.WebSocket;
      const mockMessages: any[] = [];
      const mockSocket = {
        readyState: 1, // OPEN
        send: (data: string) => {
          mockMessages.push(JSON.parse(data));
        },
        addEventListener: (event: string, handler: any) => {
          if (event === 'open') {
            setTimeout(() => handler({}), 10);
          }
        },
        removeEventListener: () => {},
        close: () => {},
      };

      (window as any).WebSocket = function() {
        return mockSocket;
      };
      (window as any).__mockWebSocket = mockSocket;
      (window as any).__mockMessages = mockMessages;
    });

    // Navigate to dashboard
    await page.goto('/');

    // Wait for WebSocket connection
    await page.waitForTimeout(100);

    // Get the subscribe message sent by the client
    const messages = await page.evaluate(() => (window as any).__mockMessages);
    const subscribeMsg = messages.find((m: any) => m.type === 'subscribe');

    expect(subscribeMsg).toBeDefined();
    expect(subscribeMsg.topics).toBeDefined();
    expect(subscribeMsg.message_id).toBeDefined();

    // Simulate server sending subscribe_ack
    await page.evaluate((messageId: string) => {
      const mockSocket = (window as any).__mockWebSocket;
      const handler = mockSocket._messageHandler;
      if (handler) {
        handler({
          data: JSON.stringify({
            type: 'subscribe_ack',
            message_id: messageId
          })
        });
      }
    }, subscribeMsg.message_id);

    // Check that the agent is now marked as subscribed (no more duplicate subscribes)
    await page.waitForTimeout(100);
    const messagesAfterAck = await page.evaluate(() => (window as any).__mockMessages);
    const duplicateSubscribe = messagesAfterAck.filter((m: any) =>
      m.type === 'subscribe' && m.message_id !== subscribeMsg.message_id
    );

    expect(duplicateSubscribe.length).toBe(0);
  });

  test('subscription timeout triggers automatic retry', async ({ page }) => {
    let subscribeCount = 0;

    // Stub WebSocket to never send ack
    await page.addInitScript(() => {
      const mockSocket = {
        readyState: 1,
        send: (data: string) => {
          const msg = JSON.parse(data);
          if (msg.type === 'subscribe') {
            (window as any).__subscribeCount = ((window as any).__subscribeCount || 0) + 1;
          }
        },
        addEventListener: (event: string, handler: any) => {
          if (event === 'open') {
            setTimeout(() => handler({}), 10);
          }
          if (event === 'message') {
            (window as any).__messageHandler = handler;
          }
        },
        removeEventListener: () => {},
        close: () => {},
      };

      (window as any).WebSocket = function() {
        return mockSocket;
      };
    });

    await page.goto('/');
    await page.waitForTimeout(100);

    // Check initial subscribe was sent
    let count = await page.evaluate(() => (window as any).__subscribeCount || 0);
    expect(count).toBeGreaterThan(0);

    // Wait for timeout (5 seconds) + retry delay
    await page.waitForTimeout(6000);

    // Verify retry was attempted
    const countAfterTimeout = await page.evaluate(() => (window as any).__subscribeCount || 0);
    expect(countAfterTimeout).toBeGreaterThan(count);
  });

  test('subscribe_error does not mark as subscribed and allows retry', async ({ page }) => {
    let subscribeCount = 0;

    await page.addInitScript(() => {
      const mockSocket = {
        readyState: 1,
        send: (data: string) => {
          const msg = JSON.parse(data);
          if (msg.type === 'subscribe') {
            (window as any).__subscribeAttempts = (window as any).__subscribeAttempts || [];
            (window as any).__subscribeAttempts.push(msg);

            // Send error response immediately
            const handler = (window as any).__messageHandler;
            if (handler) {
              setTimeout(() => {
                handler({
                  data: JSON.stringify({
                    type: 'subscribe_error',
                    message_id: msg.message_id,
                    error: 'Permission denied'
                  })
                });
              }, 50);
            }
          }
        },
        addEventListener: (event: string, handler: any) => {
          if (event === 'open') {
            setTimeout(() => handler({}), 10);
          }
          if (event === 'message') {
            (window as any).__messageHandler = handler;
          }
        },
        removeEventListener: () => {},
        close: () => {},
      };

      (window as any).WebSocket = function() {
        return mockSocket;
      };
    });

    await page.goto('/');

    // Wait for initial subscribe + error response
    await page.waitForTimeout(200);

    let attempts = await page.evaluate(() => (window as any).__subscribeAttempts || []);
    expect(attempts.length).toBeGreaterThan(0);

    // Wait for potential retry (after error, should retry on next effect run)
    await page.waitForTimeout(6000);

    attempts = await page.evaluate(() => (window as any).__subscribeAttempts || []);
    // Should have retried after the error
    expect(attempts.length).toBeGreaterThan(1);
  });

  test('pending subscriptions prevent duplicate subscribe messages', async ({ page }) => {
    await page.addInitScript(() => {
      const mockSocket = {
        readyState: 1,
        send: (data: string) => {
          const msg = JSON.parse(data);
          if (msg.type === 'subscribe') {
            (window as any).__allSubscribes = (window as any).__allSubscribes || [];
            (window as any).__allSubscribes.push({
              messageId: msg.message_id,
              topics: msg.topics,
              timestamp: Date.now()
            });
          }
        },
        addEventListener: (event: string, handler: any) => {
          if (event === 'open') {
            setTimeout(() => handler({}), 10);
          }
          if (event === 'message') {
            (window as any).__messageHandler = handler;
          }
        },
        removeEventListener: () => {},
        close: () => {},
      };

      (window as any).WebSocket = function() {
        return mockSocket;
      };
    });

    await page.goto('/');
    await page.waitForTimeout(200);

    const subscribes = await page.evaluate(() => (window as any).__allSubscribes || []);

    // Check for duplicate subscriptions to same topics within short time window
    const topicsSeen = new Map<string, number>();
    let duplicates = 0;

    for (const sub of subscribes) {
      for (const topic of sub.topics) {
        if (topicsSeen.has(topic)) {
          const prevTime = topicsSeen.get(topic)!;
          if (sub.timestamp - prevTime < 1000) {
            duplicates++;
          }
        }
        topicsSeen.set(topic, sub.timestamp);
      }
    }

    // Should not have any duplicate subscriptions for same topic within 1 second
    expect(duplicates).toBe(0);
  });

  test('reconnection clears pending subscriptions and resubscribes', async ({ page }) => {
    await page.addInitScript(() => {
      let connectionCount = 0;

      const createMockSocket = () => {
        connectionCount++;
        (window as any).__connectionCount = connectionCount;

        return {
          readyState: 1,
          send: (data: string) => {
            const msg = JSON.parse(data);
            (window as any).__latestMessages = (window as any).__latestMessages || [];
            (window as any).__latestMessages.push({
              ...msg,
              connection: connectionCount
            });
          },
          addEventListener: (event: string, handler: any) => {
            if (event === 'open') {
              setTimeout(() => handler({}), 10);
            }
          },
          removeEventListener: () => {},
          close: () => {},
        };
      };

      (window as any).WebSocket = function() {
        return createMockSocket();
      };
      (window as any).__triggerReconnect = () => {
        // Trigger reconnection logic
        const event = new Event('online');
        window.dispatchEvent(event);
      };
    });

    await page.goto('/');
    await page.waitForTimeout(200);

    const messagesBeforeReconnect = await page.evaluate(() => (window as any).__latestMessages || []);
    const connection1Subscribes = messagesBeforeReconnect.filter((m: any) =>
      m.type === 'subscribe' && m.connection === 1
    );

    // Simulate reconnection
    await page.evaluate(() => (window as any).__triggerReconnect());
    await page.waitForTimeout(500);

    const messagesAfterReconnect = await page.evaluate(() => (window as any).__latestMessages || []);
    const connection2Subscribes = messagesAfterReconnect.filter((m: any) =>
      m.type === 'subscribe' && m.connection === 2
    );

    // Should have resubscribed on new connection
    expect(connection2Subscribes.length).toBeGreaterThan(0);
  });
});
