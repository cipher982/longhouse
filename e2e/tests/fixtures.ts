import { test as base, expect, BrowserContext, type Page } from '@playwright/test';

export type { Page };

// ---------------------------------------------------------------------------
// Shared Playwright *test* object that injects the `X-Test-Worker` header into
// local browser requests and appends `worker=<id>` to every WebSocket URL
// opened by the front-end. All
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
  workerId: string;
};

function shouldInjectWorkerHeader(requestUrl: string): boolean {
  try {
    const { protocol, hostname } = new URL(requestUrl);
    return (protocol === 'http:' || protocol === 'https:') && (hostname === '127.0.0.1' || hostname === 'localhost');
  } catch {
    return false;
  }
}

async function installWorkerHeaderRouting(context: BrowserContext, workerId: string): Promise<void> {
  const continueRoute = async (
    route: import("@playwright/test").Route,
    overrides?: { headers?: Record<string, string> },
  ): Promise<void> => {
    try {
      if (overrides) {
        await route.continue(overrides);
      } else {
        await route.continue();
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("Route is already handled")) {
        return;
      }
      throw error;
    }
  };

  await context.route('**/*', async route => {
    const request = route.request();
    if (!shouldInjectWorkerHeader(request.url())) {
      await continueRoute(route);
      return;
    }

    await continueRoute(route, {
      headers: {
        ...request.headers(),
        'X-Test-Worker': workerId,
      },
    });
  });
}

async function mintDeviceToken(
  request: import('@playwright/test').APIRequestContext,
  deviceId: string,
): Promise<string> {
  const response = await request.post('/api/devices/tokens', {
    data: { device_id: deviceId },
  });
  if (!response.ok()) {
    throw new Error(
      `Failed to mint device token for E2E context: ${response.status()} ${await response.text()}`,
    );
  }

  const payload = await response.json();
  const token = payload?.token;
  if (typeof token !== 'string' || !token.startsWith('zdt_')) {
    throw new Error(`Invalid device token bootstrap payload: ${JSON.stringify(payload)}`);
  }
  return token;
}

export const test = base.extend<TestFixtures>({
  backendUrl: async ({}, use) => {
    const basePort = getBackendPort();
    await use(`http://127.0.0.1:${basePort}`);
  },

  workerId: async ({}, use, testInfo) => {
    await use(String(testInfo.parallelIndex));
  },

  request: async ({ playwright, backendUrl }, use, testInfo) => {
    // Use parallelIndex (0 to workers-1) instead of workerIndex.
    // workerIndex can exceed the configured worker count when Playwright
    // restarts workers after test failures/timeouts.
    const workerId = String(testInfo.parallelIndex);
    const bootstrap = await playwright.request.newContext({
      baseURL: backendUrl, // Use dynamic backend URL
      extraHTTPHeaders: {
        'X-Test-Worker': workerId,
      },
      // Increase timeout for API requests - reset-database can be slow under parallel load
      timeout: 30_000,
    });
    const deviceToken = await mintDeviceToken(bootstrap, `playwright-${workerId}`);

    const request = await playwright.request.newContext({
      baseURL: backendUrl,
      extraHTTPHeaders: {
        'X-Test-Worker': workerId,
        'X-Agents-Token': deviceToken,
      },
      timeout: 30_000,
    });

    await use(request);
    await request.dispose();
    await bootstrap.dispose();
  },

  context: async ({ browser }, use, testInfo) => {
    const workerId = String(testInfo.parallelIndex);

    const context = await browser.newContext();
    await installWorkerHeaderRouting(context, workerId);

    const reactBaseUrl = process.env.PLAYWRIGHT_FRONTEND_BASE || 'http://localhost:3000';

    await context.addInitScript((config: { baseUrl: string, workerId: string }) => {
      (window as any).__TEST_WORKER_ID__ = config.workerId;
      try {
        const normalized = config.baseUrl.replace(/\/$/, '');
        window.localStorage.setItem('zerg_use_react_dashboard', '1');
        window.localStorage.setItem('zerg_use_react_chat', '1');
        window.localStorage.setItem('zerg_react_automations_url', `${normalized}/automations`);

        // Add test JWT token for React authentication
        window.localStorage.setItem('zerg_jwt', 'test-jwt-token-for-e2e-tests');

        } catch (error) {
          // If localStorage is unavailable (unlikely), continue without failing tests.
          console.warn('Playwright init: unable to seed React flags', error);
        }
      }, { baseUrl: reactBaseUrl, workerId });

    // -------------------------------------------------------------------
    // Monkey-patch *browser.newContext* so ad-hoc contexts created **inside**
    // a spec inherit the worker header automatically (see realtime_updates
    // tests that open multiple tabs).
    // -------------------------------------------------------------------
    const originalNewContext = browser.newContext.bind(browser);
    // Type-cast via immediate IIFE to keep TypeScript happy.
    browser.newContext = (async (options: any = {}) => {
      const childContext = await originalNewContext(options);
      await installWorkerHeaderRouting(childContext, workerId);
      return childContext;
    }) as any;

    // ---------------------------------------------------------------------
    // runtime patch – prepend `worker=<id>` to every WebSocket URL so the
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
          url = `${url}${sep}worker=${wid}`;
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
    }, workerId);

    await use(context);

    await context.close();
  },

  // Re-export the *page* fixture so spec files work unchanged beyond the
  // import path.  “base” already provides the page linked to our custom
  // context.
});

export { expect } from '@playwright/test';
