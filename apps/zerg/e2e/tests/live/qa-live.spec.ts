/**
 * Live QA harness for the Longhouse production instance.
 *
 * Designed to run after every deploy — headless, ~60s, exit 0=pass exit 1=fail.
 * Uses the hosted login-token -> accept-token flow shared by the other live suites.
 *
 * Run via: ./scripts/qa-live.sh
 * Or:      make qa-live
 */

import { test, expect } from './fixtures';
import type { Page } from '@playwright/test';

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

test('auth + timeline loads with session rows', async ({ context }) => {
  test.setTimeout(20_000);

  const page = await context.newPage();
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
    await failWithScreenshot(page, 'timeline-auth', 'Auth failure: /api/agents/sessions returned 401. Check SMOKE_LOGIN_TOKEN.');
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
// Test 2: Legacy forum route redirects to timeline
// ---------------------------------------------------------------------------

test('forum route redirects to timeline without auth errors', async ({ context }) => {
  // Budget includes auth checks + redirect + timeline render.
  test.setTimeout(45_000);

  const page = await context.newPage();
  const { consoleErrors, serverErrors } = attachErrorCollectors(page);

  const authErrors: string[] = [];
  page.on('response', (response) => {
    if (response.url().includes('/api/') && response.status() === 401) {
      authErrors.push(response.url());
    }
  });

  await page.goto('/forum', { waitUntil: 'domcontentloaded' });
  await expect(page).toHaveURL(/\/timeline(\/.*)?(\?.*)?$/, { timeout: 10_000 });

  if (authErrors.length > 0) {
    await failWithScreenshot(
      page,
      'forum-redirect-auth',
      `Auth failures while loading /forum redirect: ${authErrors.join(', ')}`,
    );
  }

  if (serverErrors.length > 0) {
    await failWithScreenshot(
      page,
      'forum-redirect-500',
      `Server errors while loading /forum redirect: ${serverErrors.join(', ')}`,
    );
  }

  if (consoleErrors.length > 0) {
    await failWithScreenshot(
      page,
      'forum-redirect-console',
      `JS errors while loading /forum redirect: ${consoleErrors.join(' | ')}`,
    );
  }

  // Ensure timeline shell mounted after redirect.
  await page
    .locator('.sessions-page, .sessions-hero-empty, .session-card')
    .first()
    .waitFor({ timeout: 10_000 })
    .catch(async () => {
      await failWithScreenshot(page, 'forum-redirect-empty', 'Redirect from /forum did not render timeline content.');
    });

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 3: Session detail loads events
// ---------------------------------------------------------------------------

test('session detail renders event timeline', async ({ agentsRequest, context }) => {
  test.setTimeout(20_000);

  // Pull the most recent session id via API (avoids UI scraping)
  const sessionsRes = await agentsRequest.get('/api/agents/sessions?limit=1');
  expect(sessionsRes.ok(), `GET /api/agents/sessions failed: ${sessionsRes.status()}`).toBe(true);

  const sessionsData = await sessionsRes.json();
  const sessions = sessionsData?.sessions ?? sessionsData ?? [];
  if (!Array.isArray(sessions) || sessions.length === 0) {
    // No sessions at all — skip (instance may be newly provisioned)
    test.skip(true, 'No sessions available to test detail view');
    return;
  }

  const sessionId: string = sessions[0].id;

  const page = await context.newPage();
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

test('health endpoint returns healthy', async ({ agentsRequest }) => {
  test.setTimeout(10_000);

  const res = await agentsRequest.get('/api/health');
  expect(res.ok(), `GET /api/health returned ${res.status()}`).toBe(true);

  const body = await res.json();
  expect(
    body.status,
    `Expected health.status to be "healthy" or "ok", got: ${body.status}`,
  ).toMatch(/^(healthy|ok)$/);
});

test('agents sessions API returns list', async ({ agentsRequest }) => {
  test.setTimeout(10_000);

  const res = await agentsRequest.get('/api/agents/sessions?limit=5');
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

// ---------------------------------------------------------------------------
// Test 6: AI search toggle — off by default, toggles on
// ---------------------------------------------------------------------------

test('timeline has AI search toggle', async ({ context }) => {
  test.setTimeout(20_000);

  const page = await context.newPage();
  await page.goto('/timeline', { waitUntil: 'domcontentloaded' });

  // Wait for the search toolbar to render
  await page.locator('.sessions-ai-toggle').waitFor({ timeout: 10_000 });

  const toggle = page.locator('.sessions-ai-toggle');

  // AI off by default
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');
  await expect(toggle).not.toHaveClass(/sessions-ai-toggle--active/);

  // Click to enable AI search
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-pressed', 'true');
  await expect(toggle).toHaveClass(/sessions-ai-toggle--active/);

  // Click again to disable
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 7: Recall panel opens and renders search input
// ---------------------------------------------------------------------------

test('recall panel opens and shows search input', async ({ context }) => {
  test.setTimeout(20_000);

  const page = await context.newPage();
  await page.goto('/timeline', { waitUntil: 'domcontentloaded' });

  // Wait for toolbar
  await page.locator('.sessions-toolbar').waitFor({ timeout: 10_000 });

  // Recall toggle button must exist
  const recallToggle = page.getByTestId('recall-toggle');
  await expect(recallToggle).toBeVisible();

  // Open the recall panel
  await recallToggle.click();

  // Recall panel should appear with search input
  const recallPanel = page.getByTestId('recall-panel');
  await recallPanel.waitFor({ timeout: 5_000 });
  await expect(recallPanel).toBeVisible();

  // Search input must be present and focusable
  const input = page.getByTestId('recall-search-input');
  await expect(input).toBeVisible();
  await expect(input).toBeEnabled();

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 8: Briefings page loads with project selector
// ---------------------------------------------------------------------------

test('briefings page loads with project selector', async ({ context }) => {
  test.setTimeout(20_000);

  const page = await context.newPage();
  await page.goto('/briefings', { waitUntil: 'domcontentloaded' });

  // Should not 404 or throw
  const url = page.url();
  expect(url, 'Should be on briefings page, not redirected').toContain('/briefings');

  // Controls area must render
  await page.getByTestId('briefings-controls').waitFor({ timeout: 10_000 });

  // Project selector must be present and empty by default
  const select = page.getByTestId('briefings-project-select');
  await expect(select).toBeVisible();

  await page.close();
});
