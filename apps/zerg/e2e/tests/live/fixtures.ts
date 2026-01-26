import { test as base, expect, type APIRequestContext, type BrowserContext } from '@playwright/test';

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required env var: ${name}`);
  }
  return value;
}

/** Type for playwright fixture's request factory */
type RequestFactory = { newContext: (options?: { baseURL?: string; timeout?: number }) => Promise<APIRequestContext> };

/**
 * Wait for the API to be healthy before running tests.
 * Polls /health until status is "ok" twice consecutively.
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
        const response = await healthRequest.get('/health');
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

    // Timeout reached - log warning but don't fail (tests may still work)
    console.warn(`[health] Timeout after ${attempt} attempts - proceeding anyway`);
  } finally {
    await healthRequest.dispose();
  }
}

function normalizeSecret(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  if ((trimmed.startsWith("'") && trimmed.endsWith("'")) || (trimmed.startsWith('"') && trimmed.endsWith('"'))) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function buildRunId(): string {
  if (process.env.E2E_RUN_ID) return process.env.E2E_RUN_ID;
  const ts = new Date().toISOString().replace(/[-:]/g, '').replace(/\..+/, '');
  const rand = Math.random().toString(36).slice(2, 8);
  return `prod-${ts}-${rand}`;
}

type LiveFixtures = {
  apiBaseUrl: string;
  runId: string;
  authToken: string;
  request: APIRequestContext;
  context: BrowserContext;
};

export const test = base.extend<LiveFixtures>({
  apiBaseUrl: async ({}, use) => {
    const apiBaseUrl = process.env.PLAYWRIGHT_API_BASE_URL || process.env.E2E_API_URL || 'https://api.swarmlet.com';
    await use(apiBaseUrl);
  },

  runId: async ({}, use) => {
    await use(buildRunId());
  },

  authToken: async ({ apiBaseUrl, runId, playwright }, use) => {
    const secret = normalizeSecret(process.env.SMOKE_TEST_SECRET);
    if (!secret) {
      test.skip(true, 'SMOKE_TEST_SECRET not set; skipping live prod E2E');
    }

    // Wait for API health before attempting auth (prevents flaky 502s during deploys)
    await waitForHealthy(playwright.request, apiBaseUrl);

    const authRequest = await playwright.request.newContext({
      baseURL: apiBaseUrl,
      extraHTTPHeaders: {
        'X-Service-Secret': secret,
        'X-Smoke-Run-Id': runId,
      },
      timeout: 30_000,
    });

    const response = await authRequest.post('/api/auth/service-login');
    if (!response.ok()) {
      const body = await response.text();
      throw new Error(`service-login failed: ${response.status()} ${body}`);
    }

    const payload = await response.json();
    if (!payload?.access_token) {
      throw new Error(`service-login missing access_token: ${JSON.stringify(payload)}`);
    }

    await use(payload.access_token);
    await authRequest.dispose();
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

  context: async ({ browser, apiBaseUrl, authToken }, use) => {
    const context = await browser.newContext();
    const apiHost = new URL(apiBaseUrl).hostname;
    const secure = apiBaseUrl.startsWith('https://');

    await context.addCookies([
      {
        name: 'swarmlet_session',
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

// Keep the test user clean between specs

test.beforeEach(async ({ request }) => {
  try {
    await request.delete('/api/jarvis/history');
  } catch {
    // Best-effort cleanup only
  }
});

export { expect } from '@playwright/test';
