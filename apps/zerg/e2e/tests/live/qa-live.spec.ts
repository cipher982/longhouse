/**
 * Live QA harness for the Longhouse production instance.
 *
 * Designed to run after every deploy — headless, ~60s, exit 0=pass exit 1=fail.
 * Uses password auth (LONGHOUSE_PASSWORD env var), NOT the service-secret path.
 *
 * Run via: ./scripts/qa-live.sh
 * Or:      make qa-live
 */

import { readFileSync } from 'fs';
import { homedir } from 'os';
import { test as base, expect, type BrowserContext, type APIRequestContext, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Auth helpers
// ---------------------------------------------------------------------------

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required env var: ${name}`);
  }
  return value;
}

/** POST /api/auth/password — returns the access_token JWT */
async function passwordLogin(
  request: APIRequestContext,
  baseUrl: string,
  password: string,
): Promise<string> {
  const res = await request.post(`${baseUrl}/api/auth/password`, {
    data: { password },
    timeout: 15_000,
  });
  if (!res.ok()) {
    const body = await res.text().catch(() => '(unreadable)');
    throw new Error(`password-login failed: ${res.status()} ${body}`);
  }
  const payload = await res.json();
  if (!payload?.access_token) {
    throw new Error(`password-login missing access_token: ${JSON.stringify(payload)}`);
  }
  return payload.access_token as string;
}

// ---------------------------------------------------------------------------
// Custom fixture type
// ---------------------------------------------------------------------------

type QaFixtures = {
  instanceUrl: string;
  password: string;
  authToken: string;       // browser JWT from password-login
  deviceToken: string;     // device token for X-Agents-Token API calls
  authedRequest: APIRequestContext;
  authedContext: BrowserContext;
};

const test = base.extend<QaFixtures>({
  instanceUrl: async ({}, use) => {
    const url = process.env.QA_BASE_URL || 'https://david010.longhouse.ai';
    await use(url.replace(/\/$/, ''));
  },

  password: async ({}, use) => {
    const pw = requireEnv('LONGHOUSE_PASSWORD');
    await use(pw.trim().replace(/^['"]|['"]$/g, ''));
  },

  authToken: async ({ instanceUrl, password, playwright }, use) => {
    const req = await playwright.request.newContext({ timeout: 20_000 });
    try {
      const token = await passwordLogin(req, instanceUrl, password);
      await use(token);
    } finally {
      await req.dispose();
    }
  },

  deviceToken: async ({}, use) => {
    // Device token for X-Agents-Token on /api/agents/* endpoints.
    // CI: set LONGHOUSE_DEVICE_TOKEN env var.
    // Dev: read from ~/.claude/longhouse-device-token (same file the engine uses).
    const token =
      process.env.LONGHOUSE_DEVICE_TOKEN ||
      (() => {
        try {
          return readFileSync(homedir() + '/.claude/longhouse-device-token', 'utf8').trim();
        } catch {
          return '';
        }
      })();
    await use(token);
  },

  authedRequest: async ({ playwright, instanceUrl, deviceToken }, use) => {
    // /api/agents/* uses X-Agents-Token (device token), not the browser JWT
    const headers: Record<string, string> = {};
    if (deviceToken) headers['X-Agents-Token'] = deviceToken;
    const ctx = await playwright.request.newContext({
      baseURL: instanceUrl,
      extraHTTPHeaders: headers,
      timeout: 30_000,
    });
    await use(ctx);
    await ctx.dispose();
  },

  authedContext: async ({ browser, instanceUrl, authToken }, use) => {
    const host = new URL(instanceUrl).hostname;
    const secure = instanceUrl.startsWith('https://');
    const ctx = await browser.newContext({ baseURL: instanceUrl });
    await ctx.addCookies([
      {
        name: 'longhouse_session',
        value: authToken,
        domain: host,
        path: '/',
        httpOnly: true,
        secure,
        sameSite: 'Lax',
      },
    ]);
    await use(ctx);
    await ctx.close();
  },
});

// ---------------------------------------------------------------------------
// Shared error collectors
// ---------------------------------------------------------------------------

// Known benign console noise to suppress (browser extensions, HMR, etc.)
const BENIGN_CONSOLE_PATTERNS = [
  /Download the React DevTools/,
  /\[HMR\]/,
  /Failed to load resource.*favicon/i,
  /Content Security Policy/,
];

/** Attach console error + 4xx/5xx response collectors to a page. */
function attachErrorCollectors(page: Page): {
  consoleErrors: string[];
  serverErrors: string[];
} {
  const consoleErrors: string[] = [];
  const serverErrors: string[] = [];

  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      const text = msg.text();
      if (!BENIGN_CONSOLE_PATTERNS.some((p) => p.test(text))) {
        consoleErrors.push(text);
      }
    }
  });

  page.on('response', (response) => {
    const url = response.url();
    const status = response.status();
    // Catch 4xx (excluding 401 — handled separately) and all 5xx
    if (url.includes('/api/') && (status >= 500 || (status >= 400 && status !== 401))) {
      serverErrors.push(`${status} ${url}`);
    }
  });

  return { consoleErrors, serverErrors };
}

/** Save a failure screenshot and throw a descriptive error. */
async function failWithScreenshot(page: Page, testName: string, message: string): Promise<never> {
  const path = `/tmp/qa-live-fail-${testName.replace(/\s+/g, '-')}.png`;
  await page.screenshot({ path, fullPage: false }).catch(() => {});
  throw new Error(`${message}\nScreenshot saved: ${path}`);
}

// ---------------------------------------------------------------------------
// Test 1: Auth + Timeline loads
// ---------------------------------------------------------------------------

test('auth + timeline loads with session rows', async ({ authedContext, instanceUrl }) => {
  test.setTimeout(20_000);

  const page = await authedContext.newPage();
  const { consoleErrors, serverErrors } = attachErrorCollectors(page);

  let authFailed = false;
  page.on('response', (response) => {
    if (response.url().includes('/api/agents/sessions') && response.status() === 401) {
      authFailed = true;
    }
  });

  await page.goto('/timeline', { waitUntil: 'domcontentloaded' });

  // Wait for the list to appear — either the session cards or an empty-state
  await page
    .locator('.session-card, .empty-state, [class*="EmptyState"]')
    .first()
    .waitFor({ timeout: 12_000 })
    .catch(async () => {
      // Maybe still loading — give the spinner a chance to go away
      await page.waitForFunction(
        () => !document.querySelector('[class*="spinner"], [class*="Spinner"], .loading'),
        { timeout: 5_000 },
      ).catch(() => {});
    });

  if (authFailed) {
    await failWithScreenshot(page, 'timeline-auth', 'Auth failure: /api/agents/sessions returned 401. Check LONGHOUSE_PASSWORD.');
  }

  if (serverErrors.length > 0) {
    await failWithScreenshot(page, 'timeline-500', `Server errors on timeline: ${serverErrors.join(', ')}`);
  }

  if (consoleErrors.length > 0) {
    await failWithScreenshot(page, 'timeline-console', `JS errors on timeline: ${consoleErrors.join(' | ')}`);
  }

  // At least one session card should be visible (this is the dev instance with real data)
  const cardCount = await page.locator('.session-card').count();
  expect(
    cardCount,
    `Expected at least 1 session card on /timeline, found ${cardCount}. Page may be broken or empty.`,
  ).toBeGreaterThan(0);

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 2: Forum page loads
// ---------------------------------------------------------------------------

test('forum page loads without auth errors', async ({ authedContext }) => {
  // Generous budget: container wait(5s) + title(5s) + row data(20s) + overhead
  // The active sessions endpoint needs DB warmup time on fresh reprovision.
  test.setTimeout(45_000);

  const page = await authedContext.newPage();
  const { consoleErrors, serverErrors } = attachErrorCollectors(page);

  const authErrors: string[] = [];
  page.on('response', (response) => {
    if (response.url().includes('/api/') && response.status() === 401) {
      authErrors.push(response.url());
    }
  });

  await page.goto('/forum', { waitUntil: 'domcontentloaded' });

  // Wait for the forum container (appears quickly once React mounts)
  await page
    .locator('.forum-map-grid, .forum-session-list, .forum-map-page')
    .first()
    .waitFor({ timeout: 5_000 })
    .catch(async () => {
      // Fallback: just wait for the spinner to clear
      await page.waitForFunction(
        () => !document.querySelector('[class*="spinner"], [class*="Spinner"]'),
        { timeout: 5_000 },
      ).catch(() => {});
    });

  if (authErrors.length > 0) {
    await failWithScreenshot(
      page,
      'forum-auth',
      `Auth failures on /forum: ${authErrors.join(', ')}`,
    );
  }

  if (serverErrors.length > 0) {
    await failWithScreenshot(
      page,
      'forum-500',
      `Server errors on /forum: ${serverErrors.join(', ')}`,
    );
  }

  if (consoleErrors.length > 0) {
    await failWithScreenshot(
      page,
      'forum-console',
      `JS errors on /forum: ${consoleErrors.join(' | ')}`,
    );
  }

  // The page title "The Forum" should be visible
  await expect(page.getByText('The Forum')).toBeVisible({ timeout: 5_000 });

  // Wait for session rows — active sessions endpoint may need a few seconds
  // after fresh instance start to warm up the DB connection pool.
  await page.locator('.forum-session-row')
    .first()
    .waitFor({ timeout: 20_000 })
    .catch(async () => {
      await failWithScreenshot(page, 'forum-empty', 'Forum page shows no session rows — data not loading or active sessions endpoint broken.');
    });

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 3: Session detail loads events
// ---------------------------------------------------------------------------

test('session detail renders event timeline', async ({ authedRequest, authedContext }) => {
  test.setTimeout(20_000);

  // Pull the most recent session id via API (avoids UI scraping)
  const sessionsRes = await authedRequest.get('/api/agents/sessions?limit=1');
  expect(sessionsRes.ok(), `GET /api/agents/sessions failed: ${sessionsRes.status()}`).toBe(true);

  const sessionsData = await sessionsRes.json();
  const sessions = sessionsData?.sessions ?? sessionsData ?? [];
  if (!Array.isArray(sessions) || sessions.length === 0) {
    // No sessions at all — skip (instance may be newly provisioned)
    test.skip(true, 'No sessions available to test detail view');
    return;
  }

  const sessionId: string = sessions[0].id;

  const page = await authedContext.newPage();
  const { consoleErrors, serverErrors } = attachErrorCollectors(page);

  await page.goto(`/timeline/${sessionId}`, { waitUntil: 'domcontentloaded' });

  // Wait for at least one event item to render
  await page
    .locator('.event-item')
    .first()
    .waitFor({ timeout: 12_000 })
    .catch(async () => {
      await failWithScreenshot(
        page,
        'session-detail',
        `No .event-item elements found for session ${sessionId}. Event timeline may be broken.`,
      );
    });

  if (serverErrors.length > 0) {
    await failWithScreenshot(
      page,
      'session-detail-500',
      `Server errors on session detail: ${serverErrors.join(', ')}`,
    );
  }

  if (consoleErrors.length > 0) {
    await failWithScreenshot(
      page,
      'session-detail-console',
      `JS errors on session detail: ${consoleErrors.join(' | ')}`,
    );
  }

  const eventCount = await page.locator('.event-item').count();
  expect(eventCount, `Expected at least 1 event item in session ${sessionId}`).toBeGreaterThan(0);

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 4: Health + API sanity
// ---------------------------------------------------------------------------

test('health endpoint returns healthy', async ({ authedRequest }) => {
  test.setTimeout(10_000);

  const res = await authedRequest.get('/api/health');
  expect(res.ok(), `GET /api/health returned ${res.status()}`).toBe(true);

  const body = await res.json();
  expect(
    body.status,
    `Expected health.status to be "healthy" or "ok", got: ${body.status}`,
  ).toMatch(/^(healthy|ok)$/);
});

test('agents sessions API returns list', async ({ authedRequest }) => {
  test.setTimeout(10_000);

  const res = await authedRequest.get('/api/agents/sessions?limit=5');
  expect(
    res.ok(),
    `GET /api/agents/sessions returned ${res.status()} — auth may be broken`,
  ).toBe(true);

  const body = await res.json();
  const sessions = body?.sessions ?? body ?? [];
  expect(
    Array.isArray(sessions),
    `Expected sessions to be an array, got: ${JSON.stringify(body).slice(0, 200)}`,
  ).toBe(true);
});
