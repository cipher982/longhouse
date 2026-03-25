import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { test as base, expect, type APIRequestContext, type BrowserContext, type StorageState } from '@playwright/test';

type RequestFactory = { newContext: (options?: { baseURL?: string; timeout?: number }) => Promise<APIRequestContext> };
const RETRYABLE_AUTH_STATUSES = new Set([408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526]);

/**
 * Wait for the API to be healthy before running tests.
 * Polls /api/health until status is "ok" twice consecutively.
 * This prevents flaky tests during deploy windows.
 */
export async function waitForHealthy(
  requestFactory: RequestFactory,
  apiBaseUrl: string,
  options: { timeoutMs?: number; intervalMs?: number; requiredConsecutive?: number } = {}
): Promise<void> {
  const { timeoutMs = 30_000, intervalMs = 2_000, requiredConsecutive = 2 } = options;
  const startTime = Date.now();
  let consecutiveOk = 0;
  let attempt = 0;

  const healthRequest = await requestFactory.newContext({
    baseURL: apiBaseUrl,
    timeout: 5_000,
  });

  try {
    while (Date.now() - startTime < timeoutMs) {
      attempt++;
      try {
        const response = await healthRequest.get('/api/health');
        if (response.ok()) {
          const data = await response.json();
          if (data.status === 'healthy' || data.status === 'ok') {
            consecutiveOk++;
            if (consecutiveOk >= requiredConsecutive) {
              console.log(`[health] Ready after ${attempt} attempts (${Date.now() - startTime}ms)`);
              return;
            }
          } else {
            consecutiveOk = 0;
          }
        } else {
          consecutiveOk = 0;
        }
      } catch {
        consecutiveOk = 0;
      }

      if (Date.now() - startTime + intervalMs < timeoutMs) {
        await new Promise((r) => setTimeout(r, intervalMs));
      }
    }

    console.warn(`[health] Timeout after ${attempt} attempts - proceeding anyway`);
  } finally {
    await healthRequest.dispose().catch(() => {});
  }
}

export function normalizeToken(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  if ((trimmed.startsWith("'") && trimmed.endsWith("'")) || (trimmed.startsWith('"') && trimmed.endsWith('"'))) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

export function readDeviceToken(): string {
  if (process.env.LONGHOUSE_DEVICE_TOKEN) {
    return process.env.LONGHOUSE_DEVICE_TOKEN.trim();
  }

  try {
    return readFileSync(`${homedir()}/.claude/longhouse-device-token`, 'utf8').trim();
  } catch {
    return '';
  }
}

async function authenticateBrowserContext(
  context: BrowserContext,
  frontendBaseUrl: string,
  loginToken: string,
): Promise<void> {
  const authPage = await context.newPage();
  const acceptTokenUrl = new URL('/api/auth/accept-token', frontendBaseUrl);
  acceptTokenUrl.searchParams.set('token', loginToken);
  acceptTokenUrl.searchParams.set('return_to', '/timeline');

  try {
    await authPage.goto(acceptTokenUrl.toString(), { waitUntil: 'domcontentloaded' });
    await authPage.waitForURL((url) => url.pathname === '/timeline', { timeout: 20_000 });

    const cookies = await context.cookies();
    const sessionCookie = cookies.find((cookie) => cookie.name === 'longhouse_session');
    const refreshCookie = cookies.find((cookie) => cookie.name === 'longhouse_refresh');
    if (!sessionCookie || !refreshCookie) {
      throw new Error(
        `browser auth bootstrap missing required cookies (session=${!!sessionCookie}, refresh=${!!refreshCookie})`,
      );
    }

    const verifyStatus = await authPage.evaluate(async () => {
      const response = await fetch('/api/auth/verify', {
        credentials: 'include',
      });
      return response.status;
    });

    if (verifyStatus !== 204) {
      throw new Error(`browser auth verification failed with status ${verifyStatus}`);
    }
  } finally {
    await authPage.close().catch(() => {});
  }
}

export async function exchangeLoginToken(
  requestFactory: RequestFactory,
  apiBaseUrl: string,
  loginToken: string
): Promise<string> {
  const authRequest = await requestFactory.newContext({
    baseURL: apiBaseUrl,
    timeout: 30_000,
  });

  try {
    let lastError = 'accept-token failed';
    for (let attempt = 1; attempt <= 5; attempt++) {
      try {
        const response = await authRequest.post('/api/auth/accept-token', {
          data: { token: loginToken },
        });

        if (!response.ok()) {
          const body = await response.text();
          lastError = `accept-token failed: ${response.status()} ${body}`;
          if (attempt < 5 && RETRYABLE_AUTH_STATUSES.has(response.status())) {
            const delayMs = attempt * 1_500;
            console.warn(`[auth] transient accept-token ${response.status()} on attempt ${attempt}/5; retrying in ${delayMs}ms`);
            await new Promise((r) => setTimeout(r, delayMs));
            continue;
          }
          throw new Error(lastError);
        }

        const payload = await response.json();
        if (!payload?.access_token) {
          throw new Error(`accept-token missing access_token: ${JSON.stringify(payload)}`);
        }

        return payload.access_token;
      } catch (error) {
        lastError = error instanceof Error ? error.message : String(error);
        if (attempt < 5) {
          const delayMs = attempt * 1_500;
          console.warn(`[auth] accept-token attempt ${attempt}/5 failed; retrying in ${delayMs}ms: ${lastError}`);
          await new Promise((r) => setTimeout(r, delayMs));
          continue;
        }
        throw new Error(lastError);
      }
    }
    throw new Error(lastError);
  } finally {
    await authRequest.dispose();
  }
}

type LiveFixtures = {
  apiBaseUrl: string;
  frontendBaseUrl: string;
  browserStorageState: StorageState;
  authToken: string;
  deviceToken: string;
  request: APIRequestContext;
  agentsRequest: APIRequestContext;
  context: BrowserContext;
};

export const test = base.extend<LiveFixtures>({
  apiBaseUrl: [async ({}, use) => {
    const apiBaseUrl = process.env.API_URL || process.env.PLAYWRIGHT_API_BASE_URL || process.env.E2E_API_URL || '';
    await use(apiBaseUrl);
  }, { scope: 'worker' }],

  frontendBaseUrl: [async ({ apiBaseUrl }, use) => {
    const frontendBaseUrl = process.env.FRONTEND_URL || process.env.PLAYWRIGHT_BASE_URL || process.env.E2E_FRONTEND_URL || apiBaseUrl;
    await use(frontendBaseUrl);
  }, { scope: 'worker' }],

  browserStorageState: [async ({ browser, frontendBaseUrl }, use) => {
    const loginToken = normalizeToken(process.env.SMOKE_LOGIN_TOKEN);
    if (!loginToken) {
      test.skip(true, 'SMOKE_LOGIN_TOKEN not set; skipping live prod E2E');
    }

    const authContext = await browser.newContext({
      baseURL: frontendBaseUrl,
    });

    try {
      await authenticateBrowserContext(authContext, frontendBaseUrl, loginToken);
      await use(await authContext.storageState());
    } finally {
      await authContext.close();
    }
  }, { scope: 'worker' }],

  authToken: [async ({ apiBaseUrl, playwright }, use) => {
    if (!process.env.RUN_LIVE_E2E) {
      test.skip(true, 'RUN_LIVE_E2E not set; skipping live prod E2E');
    }

    if (!apiBaseUrl) {
      test.skip(true, 'API_URL or PLAYWRIGHT_API_BASE_URL required; skipping live prod E2E');
    }

    const loginToken = normalizeToken(process.env.SMOKE_LOGIN_TOKEN);
    if (!loginToken) {
      test.skip(true, 'SMOKE_LOGIN_TOKEN not set; skipping live prod E2E');
    }

    await waitForHealthy(playwright.request, apiBaseUrl);
    const accessToken = await exchangeLoginToken(playwright.request, apiBaseUrl, loginToken);
    await use(accessToken);
  }, { scope: 'worker' }],

  deviceToken: [async ({}, use) => {
    await use(readDeviceToken());
  }, { scope: 'worker' }],

  request: async ({ playwright, apiBaseUrl, authToken }, use) => {
    const request = await playwright.request.newContext({
      baseURL: apiBaseUrl,
      extraHTTPHeaders: {
        Authorization: `Bearer ${authToken}`,
      },
      timeout: 45_000,
    });
    await use(request);
    await request.dispose();
  },

  agentsRequest: async ({ playwright, apiBaseUrl, deviceToken }, use) => {
    // Hosted auth is intentionally split today:
    // - `/api/agents/*` works with the device token header.
    // - Browser navigation is validated separately via the longhouse_session cookie.
    // Keep API-side session discovery on the device token path until hosted browser auth
    // can list agents sessions without a 403.
    const extraHTTPHeaders: Record<string, string> = {};
    if (deviceToken) {
      extraHTTPHeaders['X-Agents-Token'] = deviceToken;
    }

    const request = await playwright.request.newContext({
      baseURL: apiBaseUrl,
      extraHTTPHeaders,
      timeout: 45_000,
    });
    await use(request);
    await request.dispose();
  },

  context: async ({ browser, frontendBaseUrl, browserStorageState }, use) => {
    const context = await browser.newContext({
      baseURL: frontendBaseUrl,
      storageState: browserStorageState,
    });

    try {
      await use(context);
    } finally {
      await context.close();
    }
  },
});

test.beforeEach(async ({ request }) => {
  try {
    await request.delete('/api/oikos/thread');
  } catch {
    // Best-effort cleanup only
  }
});

export { expect } from '@playwright/test';
