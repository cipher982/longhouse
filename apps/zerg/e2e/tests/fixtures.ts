import { test as base, expect, BrowserContext, type Page } from '@playwright/test';

export type { Page };

// ---------------------------------------------------------------------------
// Shared Playwright *test* object that injects the `X-Test-Commis` header *and*
// appends `commis=<id>` to every WebSocket URL opened by the front-end.  All
// existing spec files can simply switch their import to:
//
//   import { test, expect } from './fixtures';
//
// No other code changes are required.
// ---------------------------------------------------------------------------

// Backend port comes from env (set by playwright.config.js random port generation)
// Explicit env vars override random ports, but we always have a value from config
function getBackendPort(): number {
  const port = parseInt(process.env.BACKEND_PORT || '');
  if (!port || isNaN(port)) {
    throw new Error('BACKEND_PORT env var required (set by playwright.config.js)');
  }
  return port;
}

type TestFixtures = {
  context: BrowserContext;
  request: import('@playwright/test').APIRequestContext;
  backendUrl: string;
  commisId: string;
};

export const test = base.extend<TestFixtures>({
  backendUrl: async ({}, use) => {
    const basePort = getBackendPort();
    await use(`http://127.0.0.1:${basePort}`);
  },

  commisId: async ({}, use, testInfo) => {
    await use(String(testInfo.parallelIndex));
  },

  request: async ({ playwright, backendUrl }, use, testInfo) => {
    // Use parallelIndex (0 to commis-1) instead of commisIndex.
    // commisIndex can exceed the configured commis count when Playwright
    // restarts commis after test failures/timeouts.
    const commisId = String(testInfo.parallelIndex);
    const request = await playwright.request.newContext({
      baseURL: backendUrl, // Use dynamic backend URL
      extraHTTPHeaders: {
        'X-Test-Commis': commisId,
      },
      // Increase timeout for API requests - reset-database can be slow under parallel load
      timeout: 30_000,
    });
    await use(request);
    await request.dispose();
  },

  context: async ({ browser }, use, testInfo) => {
    const commisId = String(testInfo.parallelIndex);

    const context = await browser.newContext({
      extraHTTPHeaders: {
        'X-Test-Commis': commisId,
      },
    });

    const reactBaseUrl = process.env.PLAYWRIGHT_FRONTEND_BASE || 'http://localhost:3000';

    await context.addInitScript((config: { baseUrl: string, commisId: string }) => {
      (window as any).__TEST_COMMIS_ID__ = config.commisId;
      try {
        const normalized = config.baseUrl.replace(/\/$/, '');
        window.localStorage.setItem('zerg_use_react_dashboard', '1');
        window.localStorage.setItem('zerg_use_react_chat', '1');
        window.localStorage.setItem('zerg_react_dashboard_url', `${normalized}/dashboard`);
        window.localStorage.setItem('zerg_react_chat_base', `${normalized}/fiche`);

        // Add test JWT token for React authentication
        window.localStorage.setItem('zerg_jwt', 'test-jwt-token-for-e2e-tests');

        } catch (error) {
          // If localStorage is unavailable (unlikely), continue without failing tests.
          console.warn('Playwright init: unable to seed React flags', error);
        }
      }, { baseUrl: reactBaseUrl, commisId });

    // -------------------------------------------------------------------
    // Monkey-patch *browser.newContext* so ad-hoc contexts created **inside**
    // a spec inherit the commis header automatically (see realtime_updates
    // tests that open multiple tabs).
    // -------------------------------------------------------------------
    const originalNewContext = browser.newContext.bind(browser);
    // Type-cast via immediate IIFE to keep TypeScript happy.
    browser.newContext = (async (options: any = {}) => {
      options.extraHTTPHeaders = {
        ...(options.extraHTTPHeaders || {}),
        'X-Test-Commis': commisId,
      };
      return originalNewContext(options);
    }) as any;

    // ---------------------------------------------------------------------
    // runtime patch – prepend `commis=<id>` to every WebSocket URL so the
    // backend can correlate the upgrade request to the correct database.
    // ---------------------------------------------------------------------
    await context.addInitScript((wid: string) => {
      const OriginalWebSocket = window.WebSocket;
      // @ts-ignore – internal helper wrapper
      // Wrap constructor in a type-asserted function expression so TS parser
      // accepts the cast.
      window.WebSocket = (function (url: string, protocols?: string | string[]) {
        try {
          const hasQuery = url.includes('?');
          const sep = hasQuery ? '&' : '?';
          url = `${url}${sep}commis=${wid}`;
        } catch {
          /* ignore – defensive */
        }
        // @ts-ignore – invoke original ctor
        return new OriginalWebSocket(url, protocols as any);
      }) as any;

      // Copy static properties (CONNECTING, OPEN, …)
      for (const key of Object.keys(OriginalWebSocket)) {
        // @ts-ignore – dynamic assignment
        (window.WebSocket as any)[key] = (OriginalWebSocket as any)[key];
      }
      (window.WebSocket as any).prototype = OriginalWebSocket.prototype;
    }, commisId);

    await use(context);

    await context.close();
  },

  // Re-export the *page* fixture so spec files work unchanged beyond the
  // import path.  “base” already provides the page linked to our custom
  // context.
});

// ---------------------------------------------------------------------------
// Oikos chat thread isolation:
// The Oikos thread is long-lived in normal usage, and per-commis DBs mean
// it can persist across tests within the same Playwright commis. Clearing it
// here keeps tests and perf assertions deterministic without requiring every
// spec to remember to do it.
// ---------------------------------------------------------------------------

test.beforeEach(async ({ request }, testInfo) => {
  try {
    const response = await request.delete('/api/oikos/history');
    if (!response.ok()) {
      // Avoid failing the entire suite if Oikos endpoints are temporarily
      // unavailable; individual chat specs should still assert correctness.
      // Note: Suppress this warning by default - it's noisy during parallel tests
    }
  } catch {
    // Silently ignore - Oikos history cleanup is best-effort, not critical
    // Individual chat specs should still assert correctness
  }
});

export { expect } from '@playwright/test';
