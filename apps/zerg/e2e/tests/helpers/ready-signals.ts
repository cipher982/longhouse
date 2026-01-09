/**
 * Ready Signals - E2E test helpers for deterministic page/component readiness
 *
 * These helpers replace arbitrary waitForTimeout() calls with event-driven waiting,
 * making tests more reliable and faster.
 *
 * Recommended Pattern (most reliable):
 * 1. waitForReadyFlag() - waits for sticky flags on window.__jarvis.ready (preferred for "ready" signals)
 * 2. waitForPageReady() - waits for data-ready="true" on body (general page readiness)
 *
 * Event-based Pattern (for async operations):
 * 3. waitForEvent() - waits for a specific EventBus event (use for long-running ops like supervisor:complete)
 * 4. emitTestEvent() - emits a test event to trigger component reactions (for testing UI responses)
 *
 * Note: Prefer sticky flags over events for "ready" signals to avoid race conditions
 * where the event fires before the test listener is attached.
 */

import { Page } from '@playwright/test';

export interface WaitForPageReadyOptions {
  /** Timeout in milliseconds (default: 10000) */
  timeout?: number;
  /** Attribute name to check (default: 'data-ready') */
  attribute?: string;
  /** Expected attribute value (default: 'true') */
  value?: string;
}

export interface WaitForEventOptions {
  /** Timeout in milliseconds (default: 10000) */
  timeout?: number;
}

export interface WaitForEventBusOptions {
  /** Timeout in milliseconds (default: 5000) */
  timeout?: number;
}

export interface WaitForReadyFlagOptions {
  /** Timeout in milliseconds (default: 10000) */
  timeout?: number;
}

/**
 * Wait for page to signal readiness via data-ready attribute on body.
 *
 * Pages/components set document.body.setAttribute('data-ready', 'true') when they're
 * fully interactive and ready for testing.
 *
 * @example
 * await waitForPageReady(page);
 * // Now safe to interact with the page
 */
export async function waitForPageReady(
  page: Page,
  options: WaitForPageReadyOptions = {}
): Promise<void> {
  const {
    timeout = 10000,
    attribute = 'data-ready',
    value = 'true'
  } = options;

  await page.waitForFunction(
    ({ attr, val }) => document.body.getAttribute(attr) === val,
    { attr: attribute, val: value },
    { timeout }
  );
}

/**
 * Wait for a sticky ready flag on window.__jarvis.ready.
 *
 * This is the PREFERRED method for waiting on "ready" signals because it avoids
 * the race condition where an event fires before the test listener is attached.
 * The app sets flags like `window.__jarvis.ready.chatReady = true` which persist
 * until the component unmounts.
 *
 * Available flags:
 * - 'chatReady' - Jarvis chat UI is mounted and interactive
 *
 * @example
 * await waitForReadyFlag(page, 'chatReady');
 * // Chat is now ready for interaction
 */
export async function waitForReadyFlag(
  page: Page,
  flagName: 'chatReady' | string,
  options: WaitForReadyFlagOptions = {}
): Promise<void> {
  const { timeout = 10000 } = options;

  await page.waitForFunction(
    ({ flag }) => {
      const w = window as any;
      return w.__jarvis?.ready?.[flag] === true;
    },
    { flag: flagName },
    { timeout }
  );
}

/**
 * Wait for the EventBus to become available (window.__jarvis.eventBus).
 *
 * This is useful after navigation to chat pages to ensure the Jarvis app is mounted
 * before attempting to use event-based waiting.
 *
 * @example
 * await chatButton.click();
 * await waitForEventBusAvailable(page);
 * // Now safe to use waitForEvent()
 */
export async function waitForEventBusAvailable(
  page: Page,
  options: WaitForEventBusOptions = {}
): Promise<void> {
  const { timeout = 5000 } = options;

  await page.waitForFunction(
    () => {
      const w = window as any;
      const bus = w.__jarvis?.eventBus;
      // Check that eventBus exists AND has the required methods
      return bus && typeof bus.on === 'function' && typeof bus.emit === 'function';
    },
    {},
    { timeout }
  );
}

/**
 * Wait for a specific EventBus event to be emitted.
 *
 * This leverages window.__jarvis.eventBus (exposed in DEV mode) to wait for
 * application events instead of using arbitrary timeouts.
 *
 * The function will poll for the eventBus to become available (it may not be
 * immediately available after navigation).
 *
 * @example
 * // Wait for chat to be ready
 * await waitForEvent(page, 'test:chat_ready');
 *
 * // Wait for supervisor to complete
 * await waitForEvent(page, 'supervisor:complete', { timeout: 30000 });
 */
export async function waitForEvent<T = unknown>(
  page: Page,
  eventName: string,
  options: WaitForEventOptions = {}
): Promise<T> {
  const { timeout = 10000 } = options;

  const result = await page.evaluate(
    ({ eventName, timeout }) => {
      return new Promise<T>((resolve, reject) => {
        const startTime = Date.now();

        // Poll for eventBus availability (may not be immediately available after navigation)
        const checkEventBus = () => {
          const w = window as any;

          if (w.__jarvis?.eventBus) {
            // eventBus is available, subscribe to event
            const remainingTime = timeout - (Date.now() - startTime);
            if (remainingTime <= 0) {
              reject(new Error(`Timeout waiting for event "${eventName}" after ${timeout}ms`));
              return;
            }

            let timeoutId: number | undefined;
            const unsubscribe = w.__jarvis.eventBus.on(eventName, (data: T) => {
              if (timeoutId !== undefined) {
                clearTimeout(timeoutId);
              }
              unsubscribe();
              resolve(data);
            });

            timeoutId = window.setTimeout(() => {
              unsubscribe();
              reject(new Error(`Timeout waiting for event "${eventName}" after ${timeout}ms`));
            }, remainingTime);
          } else if (Date.now() - startTime > timeout) {
            reject(new Error(`EventBus not available after ${timeout}ms. Ensure app is running in DEV mode.`));
          } else {
            // Poll again in 50ms
            setTimeout(checkEventBus, 50);
          }
        };

        checkEventBus();
      });
    },
    { eventName, timeout }
  );

  return result as T;
}

/**
 * Emit a test event via EventBus to trigger component reactions.
 *
 * Use this to test UI responses to events without needing the actual backend
 * to produce them. Only works in DEV mode where window.__jarvis.eventBus is exposed.
 *
 * @example
 * // Simulate supervisor completing
 * await emitTestEvent(page, 'supervisor:complete', {
 *   runId: 1,
 *   result: 'Test complete',
 *   status: 'success',
 *   timestamp: Date.now()
 * });
 */
export async function emitTestEvent<T = unknown>(
  page: Page,
  eventName: string,
  data: T
): Promise<void> {
  await page.evaluate(
    ({ eventName, data }) => {
      const w = window as any;

      if (!w.__jarvis?.eventBus) {
        throw new Error('EventBus not available. Ensure app is running in DEV mode.');
      }

      w.__jarvis.eventBus.emit(eventName, data);
    },
    { eventName, data }
  );
}

/**
 * Wait for multiple conditions to be true.
 *
 * Combines waitForPageReady and waitForEvent patterns for complex readiness checks.
 *
 * @example
 * await waitForAllReady(page, {
 *   pageReady: true,
 *   events: ['test:chat_ready', 'test:messages_loaded']
 * });
 */
export async function waitForAllReady(
  page: Page,
  conditions: {
    pageReady?: boolean | WaitForPageReadyOptions;
    events?: string[];
    timeout?: number;
  }
): Promise<void> {
  const { pageReady, events = [], timeout = 10000 } = conditions;

  const promises: Promise<unknown>[] = [];

  if (pageReady) {
    const options = typeof pageReady === 'object' ? pageReady : {};
    promises.push(waitForPageReady(page, { ...options, timeout }));
  }

  for (const eventName of events) {
    promises.push(waitForEvent(page, eventName, { timeout }));
  }

  await Promise.all(promises);
}

/**
 * Check if a page/component is ready without waiting.
 *
 * Useful for conditional logic or polling scenarios.
 *
 * @example
 * if (await isPageReady(page)) {
 *   // Page is ready, proceed
 * }
 */
export async function isPageReady(
  page: Page,
  options: Omit<WaitForPageReadyOptions, 'timeout'> = {}
): Promise<boolean> {
  const { attribute = 'data-ready', value = 'true' } = options;

  return await page.evaluate(
    ({ attr, val }) => document.body.getAttribute(attr) === val,
    { attr: attribute, val: value }
  );
}
