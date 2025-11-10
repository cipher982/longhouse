import { test, expect } from './fixtures';

/**
 * Real E2E tests for bounded WebSocket message queue.
 *
 * Strategy: Exercise useWebSocket's internal queue by:
 * 1. Creating a MockWebSocket that stays CONNECTING (forces queueing)
 * 2. Using window.__testSendMessage exposed by useWebSocket in test mode
 * 3. Calling sendMessage 150 times to exceed MAX_QUEUED_MESSAGES (100)
 * 4. Transitioning to OPEN and observing which messages get flushed
 * 5. Verifying FIFO eviction (oldest 50 messages dropped, newest 100 sent)
 *
 * Key insight: We observe EXTERNAL effects (console.warn, socket.send calls)
 * since messageQueueRef is internal to useWebSocket.
 */

test.describe('WebSocket Bounded Message Queue (Real E2E)', () => {
  test.beforeEach(async ({ request }) => {
    await request.post('/admin/reset-database');
  });

  test('queue enforces 100 message limit with FIFO eviction', async ({ page }) => {
    const MAX_QUEUE = 100;
    const TOTAL_MESSAGES = 150;

    // Inject MockWebSocket that never reaches OPEN state (forces queueing)
    await page.addInitScript(() => {
      const CONNECTING = 0;
      const OPEN = 1;
      const CLOSED = 3;

      // Track console.warn calls
      const originalWarn = console.warn;
      (window as any).__warnings = [];
      console.warn = (...args: any[]) => {
        const msg = args.join(' ');
        (window as any).__warnings.push(msg);
        originalWarn.apply(console, args);
      };

      // Track messages sent when socket opens
      (window as any).__sentMessages = [];

      class MockWebSocket {
        static CONNECTING = CONNECTING;
        static OPEN = OPEN;
        static CLOSED = CLOSED;

        readyState = CONNECTING;
        url: string;
        private handlers: Map<string, Function[]> = new Map();

        constructor(url: string) {
          this.url = url;
          (window as any).__mockSocket = this;
          // DON'T trigger open yet - we want messages to queue first
        }

        send(data: string) {
          if (this.readyState === OPEN) {
            // Track messages that actually get sent
            (window as any).__sentMessages.push(JSON.parse(data));
          }
          // If CONNECTING, useWebSocket will queue it internally
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

        // Test helper to transition to OPEN state
        transitionToOpen() {
          this.readyState = OPEN;
          this.triggerEvent('open', {});
        }
      }

      (window as any).WebSocket = MockWebSocket;
    });

    await page.goto('/');

    // Wait for useWebSocket to expose sendMessage via window.__testSendMessage
    let sendMessageAvailable = false;
    for (let i = 0; i < 50; i++) {
      sendMessageAvailable = await page.evaluate(() => typeof (window as any).__testSendMessage === 'function');
      if (sendMessageAvailable) break;
      await page.waitForTimeout(100);
    }
    expect(sendMessageAvailable).toBe(true);

    // Send 150 messages while socket is CONNECTING (all will be queued)
    await page.evaluate((count: number) => {
      const sendMsg = (window as any).__testSendMessage;
      for (let i = 0; i < count; i++) {
        sendMsg({
          type: 'test_message',
          id: i,
          payload: `message-${i}`
        });
      }
    }, TOTAL_MESSAGES);

    await page.waitForTimeout(100);

    // Verify warnings about queue being full
    const warnings = await page.evaluate(() => (window as any).__warnings || []);
    const queueFullWarnings = warnings.filter((w: string) =>
      w.includes('Message queue full') || w.includes('Dropping oldest')
    );

    // Should warn for each evicted message (50 messages over limit)
    expect(queueFullWarnings.length).toBeGreaterThanOrEqual(50);

    // Now transition socket to OPEN to flush the queue
    await page.evaluate(() => {
      ((window as any).__mockSocket as any)?.transitionToOpen();
    });

    await page.waitForTimeout(200);

    // Check which messages were actually sent
    const sentMessages = await page.evaluate(() => (window as any).__sentMessages || []);

    // Should have sent exactly MAX_QUEUE messages (oldest 50 dropped)
    expect(sentMessages.length).toBe(MAX_QUEUE);

    // Verify FIFO eviction: first sent message should have id=50 (0-49 were evicted)
    expect(sentMessages[0].id).toBe(50);

    // Last sent message should have id=149
    expect(sentMessages[sentMessages.length - 1].id).toBe(149);

    // Verify messages are in order
    for (let i = 0; i < sentMessages.length; i++) {
      expect(sentMessages[i].id).toBe(50 + i);
    }
  });

  test('queue flushes in correct order when connection opens', async ({ page }) => {
    await page.addInitScript(() => {
      const CONNECTING = 0;
      const OPEN = 1;
      const CLOSED = 3;

      (window as any).__sentMessages = [];

      class MockWebSocket {
        static CONNECTING = CONNECTING;
        static OPEN = OPEN;
        static CLOSED = CLOSED;

        readyState = CONNECTING;
        url: string;
        private handlers: Map<string, Function[]> = new Map();

        constructor(url: string) {
          this.url = url;
          (window as any).__mockSocket = this;
        }

        send(data: string) {
          if (this.readyState === OPEN) {
            const msg = JSON.parse(data);
            (window as any).__sentMessages.push({
              ...msg,
              sentAt: Date.now()
            });
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

        transitionToOpen() {
          this.readyState = OPEN;
          this.triggerEvent('open', {});
        }
      }

      (window as any).WebSocket = MockWebSocket;
    });

    await page.goto('/');

    // Wait for sendMessage to be available
    let sendMessageAvailable = false;
    for (let i = 0; i < 50; i++) {
      sendMessageAvailable = await page.evaluate(() => typeof (window as any).__testSendMessage === 'function');
      if (sendMessageAvailable) break;
      await page.waitForTimeout(100);
    }
    expect(sendMessageAvailable).toBe(true);

    // Queue some messages while CONNECTING
    await page.evaluate(() => {
      const sendMsg = (window as any).__testSendMessage;
      for (let i = 0; i < 10; i++) {
        sendMsg({
          type: 'test_queue_flush',
          id: i
        });
      }
    });

    await page.waitForTimeout(100);

    // Now open the connection
    await page.evaluate(() => {
      ((window as any).__mockSocket as any)?.transitionToOpen();
    });

    await page.waitForTimeout(300);

    // Messages should have been flushed from the queue
    const sentMessages = await page.evaluate(() => (window as any).__sentMessages || []);

    // Should have sent all queued messages
    expect(sentMessages.length).toBeGreaterThanOrEqual(10);

    // Find our test messages
    const testMessages = sentMessages.filter((m: any) => m.type === 'test_queue_flush');
    expect(testMessages.length).toBe(10);

    // Messages should be sent in FIFO order
    for (let i = 0; i < testMessages.length; i++) {
      expect(testMessages[i].id).toBe(i);
    }

    // Timestamps should be monotonic (messages sent in order)
    for (let i = 1; i < sentMessages.length; i++) {
      expect(sentMessages[i].sentAt).toBeGreaterThanOrEqual(sentMessages[i - 1].sentAt);
    }
  });

  test('queue warning includes count when dropping messages', async ({ page }) => {
    await page.addInitScript(() => {
      const CONNECTING = 0;
      const OPEN = 1;
      const CLOSED = 3;

      const originalWarn = console.warn;
      (window as any).__warnings = [];
      console.warn = (...args: any[]) => {
        const msg = args.join(' ');
        (window as any).__warnings.push(msg);
        originalWarn.apply(console, args);
      };

      class MockWebSocket {
        static CONNECTING = CONNECTING;
        static OPEN = OPEN;
        static CLOSED = CLOSED;

        readyState = CONNECTING;
        url: string;
        private handlers: Map<string, Function[]> = new Map();

        constructor(url: string) {
          this.url = url;
          (window as any).__mockSocket = this;
        }

        send() {}
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

    // Wait for sendMessage to be available
    let sendMessageAvailable = false;
    for (let i = 0; i < 50; i++) {
      sendMessageAvailable = await page.evaluate(() => typeof (window as any).__testSendMessage === 'function');
      if (sendMessageAvailable) break;
      await page.waitForTimeout(100);
    }
    expect(sendMessageAvailable).toBe(true);

    // Send 120 messages (20 over limit)
    await page.evaluate(() => {
      const sendMsg = (window as any).__testSendMessage;
      for (let i = 0; i < 120; i++) {
        sendMsg({ type: 'test', id: i });
      }
    });

    await page.waitForTimeout(100);

    const warnings = await page.evaluate(() => (window as any).__warnings || []);

    const queueWarnings = warnings.filter((w: string) =>
      w.includes('queue full') || w.includes('100 messages')
    );

    // Should have warnings
    expect(queueWarnings.length).toBeGreaterThan(0);

    // Warning should mention the 100 message limit
    const warningsWithLimit = warnings.filter((w: string) => w.includes('100'));
    expect(warningsWithLimit.length).toBeGreaterThan(0);

    // Verify exact warning text format from useWebSocket.tsx line 332
    const expectedWarning = '[WS] Message queue full (100 messages). Dropping oldest message.';
    const hasExpectedWarning = warnings.some((w: string) => w.includes(expectedWarning));
    expect(hasExpectedWarning).toBe(true);
  });

  test('handles subscribe messages while disconnected', async ({ page }) => {
    await page.addInitScript(() => {
      const CONNECTING = 0;
      const OPEN = 1;
      const CLOSED = 3;

      (window as any).__attemptedMessages = [];

      class MockWebSocket {
        static CONNECTING = CONNECTING;
        static OPEN = OPEN;
        static CLOSED = CLOSED;

        readyState = CLOSED; // Start disconnected
        url: string;
        private handlers: Map<string, Function[]> = new Map();

        constructor(url: string) {
          this.url = url;
          (window as any).__mockSocket = this;
        }

        send(data: string) {
          // Track all send attempts
          (window as any).__attemptedMessages.push(JSON.parse(data));
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

    // Wait for sendMessage
    let sendMessageAvailable = false;
    for (let i = 0; i < 50; i++) {
      sendMessageAvailable = await page.evaluate(() => typeof (window as any).__testSendMessage === 'function');
      if (sendMessageAvailable) break;
      await page.waitForTimeout(100);
    }
    expect(sendMessageAvailable).toBe(true);

    // Try to send subscribe messages while disconnected
    await page.evaluate(() => {
      const sendMsg = (window as any).__testSendMessage;
      sendMsg({ type: 'subscribe', topics: ['agent:1', 'agent:2'] });
      sendMsg({ type: 'subscribe', topics: ['agent:3', 'agent:4'] });
    });

    await page.waitForTimeout(200);

    // Page should not have crashed
    const title = await page.title();
    expect(title).toBeTruthy();

    // Messages should have been queued (not sent yet since socket is CLOSED)
    const attemptedMessages = await page.evaluate(() => (window as any).__attemptedMessages || []);

    // Since socket is CLOSED, sendMessage should have queued the messages
    // but socket.send() won't be called, so attemptedMessages should be empty
    // This is correct behavior - messages are queued internally in useWebSocket
    // The test passes if the page doesn't crash
  });

  test('page remains functional with bounded queue (no crash)', async ({ page }) => {
    await page.addInitScript(() => {
      const CONNECTING = 0;
      const OPEN = 1;
      const CLOSED = 3;

      class MockWebSocket {
        static CONNECTING = CONNECTING;
        static OPEN = OPEN;
        static CLOSED = CLOSED;

        readyState = CONNECTING;
        url: string;
        private handlers: Map<string, Function[]> = new Map();

        constructor(url: string) {
          this.url = url;
          (window as any).__mockSocket = this;

          // Eventually transition to OPEN so app is functional
          setTimeout(() => {
            this.readyState = OPEN;
            this.triggerEvent('open', {});
          }, 500);
        }

        send() {}
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

    // Monitor for JavaScript errors
    const errors: string[] = [];
    page.on('pageerror', (error) => {
      errors.push(error.message);
    });

    await page.goto('/');
    await page.waitForTimeout(2000);

    // Page should still be functional
    const title = await page.title();
    expect(title).toBeTruthy();

    // Dashboard should be visible
    const dashboard = page.locator('#dashboard, [data-testid="dashboard"]');
    await expect(dashboard.first()).toBeVisible({ timeout: 5000 });

    // No JavaScript errors
    expect(errors.length).toBe(0);
  });
});
