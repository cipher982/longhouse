import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { test as base, expect, type APIRequestContext, type BrowserContext } from '@playwright/test';

type RequestFactory = { newContext: (options?: { baseURL?: string; timeout?: number }) => Promise<APIRequestContext> };

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
    await healthRequest.dispose();
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
    const response = await authRequest.post('/api/auth/accept-token', {
      data: { token: loginToken },
    });

    if (!response.ok()) {
      const body = await response.text();
      throw new Error(`accept-token failed: ${response.status()} ${body}`);
    }

    const payload = await response.json();
    if (!payload?.access_token) {
      throw new Error(`accept-token missing access_token: ${JSON.stringify(payload)}`);
    }

    return payload.access_token;
  } finally {
    await authRequest.dispose();
  }
}

type LiveFixtures = {
  apiBaseUrl: string;
  frontendBaseUrl: string;
  authToken: string;
  deviceToken: string;
  request: APIRequestContext;
  agentsRequest: APIRequestContext;
  context: BrowserContext;
};

export const test = base.extend<LiveFixtures>({
  apiBaseUrl: async ({}, use) => {
    const apiBaseUrl = process.env.API_URL || process.env.PLAYWRIGHT_API_BASE_URL || process.env.E2E_API_URL || '';
    await use(apiBaseUrl);
  },

  frontendBaseUrl: async ({ apiBaseUrl }, use) => {
    const frontendBaseUrl = process.env.FRONTEND_URL || process.env.PLAYWRIGHT_BASE_URL || process.env.E2E_FRONTEND_URL || apiBaseUrl;
    await use(frontendBaseUrl);
  },

  authToken: async ({ apiBaseUrl, playwright }, use) => {
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
  },

  deviceToken: async ({}, use) => {
    await use(readDeviceToken());
  },

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

  context: async ({ browser, apiBaseUrl, frontendBaseUrl, authToken }, use) => {
    const context = await browser.newContext({
      baseURL: frontendBaseUrl,
    });
    const apiHost = new URL(apiBaseUrl).hostname;
    const secure = apiBaseUrl.startsWith('https://');

    await context.addCookies([
      {
        name: 'longhouse_session',
        value: authToken,
        domain: apiHost,
        path: '/',
        httpOnly: true,
        secure,
        sameSite: 'Lax',
      },
    ]);

    await use(context);
    await context.close();
  },
});

test.beforeEach(async ({ request }) => {
  try {
    await request.delete('/api/oikos/history');
  } catch {
    // Best-effort cleanup only
  }
});

export { expect } from '@playwright/test';
