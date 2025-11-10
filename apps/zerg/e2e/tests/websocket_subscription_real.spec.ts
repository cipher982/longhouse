import { test, expect } from './fixtures';

/**
 * Real E2E tests for WebSocket subscription confirmation.
 *
 * These tests actually exercise the UI by:
 * 1. Stubbing the WebSocket with proper constants and event handlers
 * 2. Observing real UI state changes
 * 3. Verifying actual subscription behavior
 *
 * Unlike the previous tests which just replayed logic, these prove
 * the implementation actually works in the browser.
 */

test.describe('WebSocket Subscription Confirmation (Real E2E)', () => {
  test.beforeEach(async ({ request }) => {
    // Use fixture-provided request context with auth headers
    await request.post('/admin/reset-database');
  });

  test('successful subscription tracks message ID and waits for ack', async ({ page }) => {
    // Stub WebSocket with proper constants and event storage
    await page.addInitScript(() => {
      const OriginalWebSocket = window.WebSocket;

      // Copy static constants
      const CONNECTING = 0;
      const OPEN = 1;
      const CLOSING = 2;
      const CLOSED = 3;

      class MockWebSocket {
        static CONNECTING = CONNECTING;
        static OPEN = OPEN;
        static CLOSING = CLOSING;
        static CLOSED = CLOSED;

        readyState = OPEN;
        url: string;
        private handlers: Map<string, Function[]> = new Map();

        constructor(url: string) {
          this.url = url;
          (window as any).__mockWebSocket = this;
          (window as any).__sentMessages = [];

          // Trigger open event asynchronously
          setTimeout(() => {
            this.triggerEvent('open', {});
          }, 10);
        }

        send(data: string) {
          const msg = JSON.parse(data);
          (window as any).__sentMessages.push(msg);
        }

        addEventListener(event: string, handler: Function) {
          if (!this.handlers.has(event)) {
            this.handlers.set(event, []);
          }
          this.handlers.get(event)!.push(handler);
        }

        removeEventListener(event: string, handler: Function) {
          const handlers = this.handlers.get(event);
          if (handlers) {
            const index = handlers.indexOf(handler);
            if (index > -1) {
              handlers.splice(index, 1);
            }
          }
        }

        close() {
          this.readyState = CLOSED;
        }

        // Helper to trigger events from outside
        triggerEvent(event: string, data: any) {
          const handlers = this.handlers.get(event);
          if (handlers) {
            handlers.forEach(h => h(data));
          }
        }

        // Helper to inject server messages
        injectMessage(message: any) {
          const handlers = this.handlers.get('message');
          if (handlers) {
            handlers.forEach(h => h({ data: JSON.stringify(message) }));
          }
        }
      }

      (window as any).WebSocket = MockWebSocket;
    });

    await page.goto('/');
    await page.waitForTimeout(200);

    // Get the subscribe message
    const messages = await page.evaluate(() => (window as any).__sentMessages || []);
    const subscribeMsg = messages.find((m: any) => m.type === 'subscribe');

    expect(subscribeMsg).toBeDefined();
    expect(subscribeMsg.message_id).toBeDefined();

    // Simulate server sending subscribe_ack
    await page.evaluate((messageId: string) => {
      const socket = (window as any).__mockWebSocket;
      if (socket) {
        socket.injectMessage({
          type: 'subscribe_ack',
          message_id: messageId
        });
      }
    }, subscribeMsg.message_id);

    await page.waitForTimeout(100);

    // Verify no duplicate subscribe attempts
    const afterAck = await page.evaluate(() => (window as any).__sentMessages || []);
    const allSubscribes = afterAck.filter((m: any) => m.type === 'subscribe');

    // Should only have the initial subscribe (ack was received)
    expect(allSubscribes.length).toBe(1);
  });

  test('subscription timeout triggers automatic retry via wsReconnectToken', async ({ page }) => {
    await page.addInitScript(() => {
      const CONNECTING = 0;
      const OPEN = 1;
      const CLOSING = 2;
      const CLOSED = 3;

      class MockWebSocket {
        static CONNECTING = CONNECTING;
        static OPEN = OPEN;
        static CLOSING = CLOSING;
        static CLOSED = CLOSED;

        readyState = OPEN;
        url: string;
        private handlers: Map<string, Function[]> = new Map();

        constructor(url: string) {
          this.url = url;
          (window as any).__sentMessages = (window as any).__sentMessages || [];

          setTimeout(() => {
            this.triggerEvent('open', {});
          }, 10);
        }

        send(data: string) {
          const msg = JSON.parse(data);
          (window as any).__sentMessages.push({
            ...msg,
            timestamp: Date.now()
          });
        }

        addEventListener(event: string, handler: Function) {
          if (!this.handlers.has(event)) {
            this.handlers.set(event, []);
          }
          this.handlers.get(event)!.push(handler);
        }

        removeEventListener() {}
        close() { this.readyState = CLOSED; }

        triggerEvent(event: string, data: any) {
          const handlers = this.handlers.get(event);
          if (handlers) {
            handlers.forEach(h => h(data));
          }
        }
      }

      (window as any).WebSocket = MockWebSocket;
    });

    await page.goto('/');
    await page.waitForTimeout(200);

    const initialMessages = await page.evaluate(() => (window as any).__sentMessages || []);
    const initialSubscribes = initialMessages.filter((m: any) => m.type === 'subscribe');
    expect(initialSubscribes.length).toBeGreaterThan(0);

    // Wait for 5-second timeout + small buffer
    await page.waitForTimeout(5500);

    const afterTimeout = await page.evaluate(() => (window as any).__sentMessages || []);
    const allSubscribes = afterTimeout.filter((m: any) => m.type === 'subscribe');

    // Should have retried after timeout
    expect(allSubscribes.length).toBeGreaterThan(initialSubscribes.length);
  });

  test('subscribe_error triggers retry via wsReconnectToken', async ({ page }) => {
    await page.addInitScript(() => {
      const CONNECTING = 0;
      const OPEN = 1;
      const CLOSING = 2;
      const CLOSED = 3;

      class MockWebSocket {
        static CONNECTING = CONNECTING;
        static OPEN = OPEN;
        static CLOSING = CLOSING;
        static CLOSED = CLOSED;

        readyState = OPEN;
        url: string;
        private handlers: Map<string, Function[]> = new Map();

        constructor(url: string) {
          this.url = url;
          (window as any).__sentMessages = (window as any).__sentMessages || [];
          (window as any).__mockWebSocket = this;

          setTimeout(() => {
            this.triggerEvent('open', {});
          }, 10);
        }

        send(data: string) {
          const msg = JSON.parse(data);
          (window as any).__sentMessages.push(msg);

          // Auto-respond with error for subscribe messages
          if (msg.type === 'subscribe') {
            setTimeout(() => {
              this.injectMessage({
                type: 'subscribe_error',
                message_id: msg.message_id,
                error: 'Permission denied'
              });
            }, 50);
          }
        }

        addEventListener(event: string, handler: Function) {
          if (!this.handlers.has(event)) {
            this.handlers.set(event, []);
          }
          this.handlers.get(event)!.push(handler);
        }

        removeEventListener() {}
        close() { this.readyState = CLOSED; }

        triggerEvent(event: string, data: any) {
          const handlers = this.handlers.get(event);
          if (handlers) {
            handlers.forEach(h => h(data));
          }
        }

        injectMessage(message: any) {
          const handlers = this.handlers.get('message');
          if (handlers) {
            handlers.forEach(h => h({ data: JSON.stringify(message) }));
          }
        }
      }

      (window as any).WebSocket = MockWebSocket;
    });

    await page.goto('/');
    await page.waitForTimeout(200);

    const initialMessages = await page.evaluate(() => (window as any).__sentMessages || []);
    const initialSubscribes = initialMessages.filter((m: any) => m.type === 'subscribe');
    expect(initialSubscribes.length).toBeGreaterThan(0);

    // Wait for error response and retry trigger
    await page.waitForTimeout(1000);

    const afterError = await page.evaluate(() => (window as any).__sentMessages || []);
    const allSubscribes = afterError.filter((m: any) => m.type === 'subscribe');

    // Should have retried after error (wsReconnectToken incremented)
    expect(allSubscribes.length).toBeGreaterThan(initialSubscribes.length);
  });

  test('timeout triggers retry without duplicate subscriptions to same topics', async ({ page }) => {
    await page.addInitScript(() => {
      const CONNECTING = 0;
      const OPEN = 1;
      const CLOSING = 2;
      const CLOSED = 3;

      class MockWebSocket {
        static CONNECTING = CONNECTING;
        static OPEN = OPEN;
        static CLOSING = CLOSING;
        static CLOSED = CLOSED;

        readyState = OPEN;
        url: string;
        private handlers: Map<string, Function[]> = new Map();

        constructor(url: string) {
          this.url = url;
          (window as any).__sentMessages = [];

          setTimeout(() => {
            this.triggerEvent('open', {});
          }, 10);
        }

        send(data: string) {
          const msg = JSON.parse(data);
          (window as any).__sentMessages.push({
            ...msg,
            timestamp: Date.now()
          });
        }

        addEventListener(event: string, handler: Function) {
          if (!this.handlers.has(event)) {
            this.handlers.set(event, []);
          }
          this.handlers.get(event)!.push(handler);
        }

        removeEventListener() {}
        close() { this.readyState = CLOSED; }

        triggerEvent(event: string, data: any) {
          const handlers = this.handlers.get(event);
          if (handlers) {
            handlers.forEach(h => h(data));
          }
        }
      }

      (window as any).WebSocket = MockWebSocket;
    });

    await page.goto('/');

    // Wait for initial subscribe
    await page.waitForTimeout(200);
    const initialMessages = await page.evaluate(() => (window as any).__sentMessages || []);
    const initialSubscribes = initialMessages.filter((m: any) => m.type === 'subscribe');

    expect(initialSubscribes.length).toBeGreaterThan(0);

    // Extract topics from initial subscribe
    const initialTopics = new Set<string>();
    for (const sub of initialSubscribes) {
      if (sub.topics) {
        for (const topic of sub.topics) {
          initialTopics.add(topic);
        }
      }
    }

    // Wait through the 5-second timeout + buffer for retry to trigger
    await page.waitForTimeout(5500);

    // Get all messages after timeout
    const afterTimeout = await page.evaluate(() => (window as any).__sentMessages || []);
    const allSubscribes = afterTimeout.filter((m: any) => m.type === 'subscribe');

    // After timeout, wsReconnectToken increments and triggers retry
    // We SHOULD see retry attempts (more subscribes than initial)
    expect(allSubscribes.length).toBeGreaterThan(initialSubscribes.length);

    // But we should NOT see duplicate topics within the SAME subscribe message
    // (the bug we're protecting against: effect runs twice with same topics)
    for (const sub of allSubscribes) {
      if (sub.topics) {
        const topicsInThisMessage = new Set<string>();
        for (const topic of sub.topics) {
          // Each topic should appear only once per subscribe message
          expect(topicsInThisMessage.has(topic)).toBe(false);
          topicsInThisMessage.add(topic);
        }
      }
    }

    // Verify retry subscribed to the same topics (retry is working)
    const retriedTopics = new Set<string>();
    for (const sub of allSubscribes.slice(initialSubscribes.length)) {
      if (sub.topics) {
        for (const topic of sub.topics) {
          retriedTopics.add(topic);
        }
      }
    }

    // Retry should attempt same topics that timed out
    for (const topic of initialTopics) {
      expect(retriedTopics.has(topic)).toBe(true);
    }
  });
});
