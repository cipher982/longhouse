/**
 * Live QA harness for the Longhouse production instance.
 *
 * Designed to run after every deploy — headless, ~60s, exit 0=pass exit 1=fail.
 * Uses the hosted login-token -> accept-token flow shared by the other live suites.
 *
 * Run via: ./scripts/qa-live.sh
 * Or:      make qa-live
 */

import { test, expect, isIgnorablePlaywrightArtifactError, normalizeToken } from "./fixtures";
import type { APIRequestContext, Page } from "@playwright/test";
import { waitForPageReady } from "../helpers/ready-signals";

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

  page.on("console", (msg) => {
    if (msg.type() === "error") {
      const text = msg.text();
      if (!BENIGN_CONSOLE_PATTERNS.some((p) => p.test(text))) {
        consoleErrors.push(text);
      }
    }
  });

  page.on("response", (response) => {
    const url = response.url();
    const status = response.status();
    // Catch 4xx (excluding 401 — handled separately) and all 5xx
    if (
      url.includes("/api/") &&
      (status >= 500 || (status >= 400 && status !== 401))
    ) {
      serverErrors.push(`${status} ${url}`);
    }
  });

  return { consoleErrors, serverErrors };
}

/** Save a failure screenshot and throw a descriptive error. */
async function failWithScreenshot(
  page: Page,
  testName: string,
  message: string,
): Promise<never> {
  const path = `/tmp/qa-live-fail-${testName.replace(/\s+/g, "-")}.png`;
  await page.screenshot({ path, fullPage: false }).catch(() => {});
  throw new Error(`${message}\nScreenshot saved: ${path}`);
}

async function waitForLivePageReady(
  page: Page,
  testName: string,
  message: string,
  timeout: number = 15_000,
): Promise<void> {
  await waitForPageReady(page, { timeout }).catch(async () => {
    await failWithScreenshot(page, testName, message);
  });
}

async function findSessionIdViaAgentsApi(request: APIRequestContext): Promise<string | null> {
  const response = await request.get("/api/agents/sessions?limit=5");
  if (!response.ok()) {
    return null;
  }

  const body = await response.json();
  const sessions = Array.isArray(body?.sessions) ? body.sessions : [];
  for (const session of sessions) {
    if (typeof session?.id === "string" && session.id.length > 0) {
      return session.id;
    }
  }

  return null;
}

function isLoopPath(pathname: string): boolean {
  return /^\/loop(?:\/.*)?$/.test(pathname);
}

// ---------------------------------------------------------------------------
// Test 1: Auth + Timeline loads
// ---------------------------------------------------------------------------

test("auth + timeline loads with session rows", async ({ context }) => {
  test.setTimeout(20_000);

  const page = await context.newPage();
  const { consoleErrors, serverErrors } = attachErrorCollectors(page);

  let authFailed = false;
  page.on("response", (response) => {
    if (
      response.url().includes("/api/timeline/sessions") &&
      response.status() === 401
    ) {
      authFailed = true;
    }
  });

  await page.goto("/timeline", { waitUntil: "domcontentloaded" });

  let timelineReady = false;
  for (let attempt = 1; attempt <= 2; attempt++) {
    const ready = await waitForPageReady(page, { timeout: 12_000 })
      .then(() => true)
      .catch(() => false);

    if (ready) {
      timelineReady = true;
      break;
    }

    if (authFailed || serverErrors.length > 0 || consoleErrors.length > 0 || attempt === 2) {
      break;
    }

    await page.reload({ waitUntil: "domcontentloaded" });
  }

  if (!timelineReady) {
    await failWithScreenshot(
      page,
      "timeline-not-ready",
      "Timeline never reached data-ready=true. The app stayed in its loading shell.",
    );
  }

  if (authFailed) {
    await failWithScreenshot(
      page,
      "timeline-auth",
      "Auth failure: /api/timeline/sessions returned 401. Check SMOKE_LOGIN_TOKEN.",
    );
  }

  if (serverErrors.length > 0) {
    await failWithScreenshot(
      page,
      "timeline-500",
      `Server errors on timeline: ${serverErrors.join(", ")}`,
    );
  }

  if (consoleErrors.length > 0) {
    await failWithScreenshot(
      page,
      "timeline-console",
      `JS errors on timeline: ${consoleErrors.join(" | ")}`,
    );
  }

  // At least one session card should be visible (this is the dev instance with real data)
  const cardCount = await page.locator(".session-card").count();
  expect(
    cardCount,
    `Expected at least 1 session card on /timeline, found ${cardCount}. Page may be broken or empty.`,
  ).toBeGreaterThan(0);

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 2: Legacy forum route redirects to timeline
// ---------------------------------------------------------------------------

test("loop login round-trip preserves loop destination", async ({
  browser,
  frontendBaseUrl,
}) => {
  test.setTimeout(30_000);

  const loginToken = normalizeToken(process.env.SMOKE_LOGIN_TOKEN);
  if (!loginToken) {
    test.skip(true, "SMOKE_LOGIN_TOKEN not set");
    return;
  }

  const baseOrigin = new URL(frontendBaseUrl).origin;
  const context = await browser.newContext({ baseURL: baseOrigin });
  const page = await context.newPage();

  try {
    // --- Part 1: Unauthenticated /loop shows login with SSO CTA ---
    await page.goto(`${baseOrigin}/loop`, { waitUntil: "domcontentloaded" });
    const loginButton = page.getByRole("button", {
      name: /continue to your longhouse account/i,
    });
    await loginButton.waitFor({ timeout: 15_000 });

    // Intercept the navigation instead of actually going to control.longhouse.ai.
    // The old test clicked the button and waited for the external domain to load,
    // which was the sole source of flakiness (network, Cloudflare, DNS).
    await page.route("**/*", (route) => {
      const url = new URL(route.request().url());
      if (url.host === "control.longhouse.ai") {
        route.abort();
      } else {
        route.continue();
      }
    });

    const [interceptedRequest] = await Promise.all([
      page.waitForRequest(
        (req) => new URL(req.url()).host === "control.longhouse.ai",
        { timeout: 15_000 },
      ),
      loginButton.click(),
    ]);

    const redirectParsed = new URL(interceptedRequest.url());
    expect(
      redirectParsed.host,
      "Login CTA should redirect to control.longhouse.ai",
    ).toBe("control.longhouse.ai");

    // Clean up route handler before continuing
    await page.unroute("**/*");

    // --- Part 2: accept-token with return_to=/loop lands on /loop ---
    await page.goto(
      `${baseOrigin}/api/auth/accept-token?token=${encodeURIComponent(loginToken)}&return_to=%2Floop`,
      { waitUntil: "domcontentloaded" },
    );
    await page.waitForURL((url) => isLoopPath(url.pathname), {
      timeout: 20_000,
    });

    const finalPath = new URL(page.url()).pathname;
    expect(
      isLoopPath(finalPath),
      `Expected hosted handoff to land on /loop or a loop child path, got ${finalPath}`,
    ).toBe(true);
    expect(finalPath, "Hosted handoff should not dump users on /timeline").not.toContain(
      "/timeline",
    );
  } catch (error) {
    await failWithScreenshot(
      page,
      "loop-auth-round-trip",
      error instanceof Error ? error.message : String(error),
    );
  } finally {
    await context.close().catch((error) => {
      if (!isIgnorablePlaywrightArtifactError(error)) {
        throw error;
      }
    });
  }
});

test("forum route redirects to timeline without auth errors", async ({
  context,
}) => {
  // Budget includes auth checks + redirect + timeline render.
  test.setTimeout(45_000);

  const page = await context.newPage();
  const { consoleErrors, serverErrors } = attachErrorCollectors(page);

  const authErrors: string[] = [];
  page.on("response", (response) => {
    if (response.url().includes("/api/") && response.status() === 401) {
      authErrors.push(response.url());
    }
  });

  await page.goto("/forum", { waitUntil: "domcontentloaded" });
  await expect(page).toHaveURL(/\/timeline(\/.*)?(\?.*)?$/, {
    timeout: 10_000,
  });

  await waitForLivePageReady(
    page,
    "forum-redirect-not-ready",
    "Redirect from /forum reached /timeline but never became interactive.",
  );

  if (authErrors.length > 0) {
    await failWithScreenshot(
      page,
      "forum-redirect-auth",
      `Auth failures while loading /forum redirect: ${authErrors.join(", ")}`,
    );
  }

  if (serverErrors.length > 0) {
    await failWithScreenshot(
      page,
      "forum-redirect-500",
      `Server errors while loading /forum redirect: ${serverErrors.join(", ")}`,
    );
  }

  if (consoleErrors.length > 0) {
    await failWithScreenshot(
      page,
      "forum-redirect-console",
      `JS errors while loading /forum redirect: ${consoleErrors.join(" | ")}`,
    );
  }

  await page
    .locator(".sessions-page, .sessions-hero-empty, .session-card")
    .first()
    .waitFor({ timeout: 10_000 })
    .catch(async () => {
      await failWithScreenshot(
        page,
        "forum-redirect-empty",
        "Redirect from /forum became ready but did not render timeline content.",
      );
    });

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 3: Session detail loads events
// ---------------------------------------------------------------------------

test("session detail renders event timeline", async ({ context, agentsRequest }) => {
  test.setTimeout(45_000);

  const sessionId = await findSessionIdViaAgentsApi(agentsRequest).catch(() => null);
  if (!sessionId) {
    test.skip(true, "No sessions available to test detail view");
    return;
  }

  const page = await context.newPage();

  const { consoleErrors, serverErrors } = attachErrorCollectors(page);
  const authErrors: string[] = [];
  const detailPath = `/api/timeline/sessions/${sessionId}`;
  const timelineItems = page.locator(
    '[data-testid="session-timeline-row"], button[id^="event-"], .timeline-row, .event-item',
  );

  page.on("response", (response) => {
    const url = response.url();
    if (
      url.includes(detailPath) &&
      (response.status() === 401 || response.status() === 403)
    ) {
      authErrors.push(`${response.status()} ${url}`);
    }
  });

  await page.goto(`/timeline/${sessionId}`, { waitUntil: "domcontentloaded" });

  await waitForLivePageReady(
    page,
    "session-detail-not-ready",
    `Session detail for ${sessionId} never reached data-ready=true.`,
  );

  if (authErrors.length > 0) {
    await failWithScreenshot(
      page,
      "session-detail-auth",
      `Auth failures on session detail: ${authErrors.join(", ")}`,
    );
  }

  // Support both the current workspace DOM and the older live session-detail shape.
  await timelineItems
    .first()
    .waitFor({ timeout: 12_000 })
    .catch(async () => {
      await failWithScreenshot(
        page,
        "session-detail",
        `No compatible timeline items found for session ${sessionId}. Expected [data-testid=\"session-timeline-row\"], button[id^=\"event-\"], .timeline-row, or .event-item.`,
      );
    });

  if (serverErrors.length > 0) {
    await failWithScreenshot(
      page,
      "session-detail-500",
      `Server errors on session detail: ${serverErrors.join(", ")}`,
    );
  }

  if (consoleErrors.length > 0) {
    await failWithScreenshot(
      page,
      "session-detail-console",
      `JS errors on session detail: ${consoleErrors.join(" | ")}`,
    );
  }

  const eventCount = await timelineItems.count();
  expect(
    eventCount,
    `Expected at least 1 compatible timeline item in session ${sessionId}`,
  ).toBeGreaterThan(0);

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 4: Health + API sanity
// ---------------------------------------------------------------------------

test("health endpoint returns healthy", async ({ agentsRequest }) => {
  test.setTimeout(10_000);

  const res = await agentsRequest.get("/api/health");
  expect(res.ok(), `GET /api/health returned ${res.status()}`).toBe(true);

  const body = await res.json();
  expect(
    body.status,
    `Expected health.status to be "healthy" or "ok", got: ${body.status}`,
  ).toMatch(/^(healthy|ok)$/);
});

test("agents sessions API returns list", async ({ agentsRequest }) => {
  test.setTimeout(10_000);

  const res = await agentsRequest.get("/api/agents/sessions?limit=5");
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

test("timeline has AI search toggle", async ({ context }) => {
  test.setTimeout(20_000);

  const page = await context.newPage();
  await page.goto("/timeline", { waitUntil: "domcontentloaded" });
  await waitForLivePageReady(
    page,
    "timeline-ai-toggle-not-ready",
    "Timeline never became interactive before checking the AI search toggle.",
  );

  // Wait for the search toolbar to render
  await page.locator(".sessions-ai-toggle").waitFor({ timeout: 10_000 });

  const toggle = page.locator(".sessions-ai-toggle");

  // AI off by default
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  await expect(toggle).not.toHaveClass(/sessions-ai-toggle--active/);

  // Click to enable AI search
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await expect(toggle).toHaveClass(/sessions-ai-toggle--active/);

  // Click again to disable
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "false");

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 7: Recall panel opens and renders search input
// ---------------------------------------------------------------------------

test("recall panel opens and shows search input", async ({ context }) => {
  test.setTimeout(20_000);

  const page = await context.newPage();
  await page.goto("/timeline", { waitUntil: "domcontentloaded" });
  await waitForLivePageReady(
    page,
    "timeline-recall-not-ready",
    "Timeline never became interactive before opening the recall panel.",
  );

  // Wait for toolbar
  await page.locator(".sessions-toolbar").waitFor({ timeout: 10_000 });

  // Recall toggle button must exist
  const recallToggle = page.getByTestId("recall-toggle");
  await expect(recallToggle).toBeVisible();

  // Open the recall panel
  await recallToggle.click();

  // Recall panel should appear with search input
  const recallPanel = page.getByTestId("recall-panel");
  await recallPanel.waitFor({ timeout: 5_000 });
  await expect(recallPanel).toBeVisible();

  // Search input must be present and focusable
  const input = page.getByTestId("recall-search-input");
  await expect(input).toBeVisible();
  await expect(input).toBeEnabled();

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 8: Briefings page loads with project selector
// ---------------------------------------------------------------------------

test("briefings page loads with project selector", async ({ context }) => {
  test.setTimeout(20_000);

  const page = await context.newPage();
  await page.goto("/briefings", { waitUntil: "domcontentloaded" });
  await waitForLivePageReady(
    page,
    "briefings-not-ready",
    "Briefings page never reached data-ready=true.",
  );

  // Should not 404 or throw
  const url = page.url();
  expect(url, "Should be on briefings page, not redirected").toContain(
    "/briefings",
  );

  // Controls area must render
  await page.getByTestId("briefings-controls").waitFor({ timeout: 10_000 });

  // Project selector must be present and empty by default
  const select = page.getByTestId("briefings-project-select");
  await expect(select).toBeVisible();

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 9: Session continuation backend is configured and ready
// ---------------------------------------------------------------------------

test("session continuation backend is ready", async ({
  playwright,
  apiBaseUrl,
  authToken,
}) => {
  test.setTimeout(10_000);

  const requestContext = await playwright.request.newContext({
    baseURL: apiBaseUrl,
    timeout: 10_000,
  });

  try {
    const res = await requestContext.get(
      `/api/sessions/continuation-readiness?token=${encodeURIComponent(authToken)}`,
    );
    expect(
      res.ok(),
      `GET /api/sessions/continuation-readiness returned ${res.status()}`,
    ).toBe(true);

    const body = await res.json();
    expect(
      body.ready,
      `Continuation not ready: backend=${body.backend}, issues=${JSON.stringify(body.issues)}`,
    ).toBe(true);
  } finally {
    await requestContext.dispose();
  }
});

// ---------------------------------------------------------------------------
// Test 10: Auth refresh endpoint works (token rotation)
// ---------------------------------------------------------------------------

test("auth refresh endpoint rotates tokens", async ({ playwright, apiBaseUrl }) => {
  test.setTimeout(10_000);

  const loginToken = process.env.SMOKE_LOGIN_TOKEN?.trim();
  if (!loginToken) {
    test.skip(true, "SMOKE_LOGIN_TOKEN not set");
    return;
  }

  // Create a fresh request context and login through accept-token to get both cookies
  const ctx = await playwright.request.newContext({
    baseURL: apiBaseUrl,
  });

  const loginRes = await ctx.post("/api/auth/accept-token", {
    data: { token: loginToken },
  });
  expect(
    loginRes.ok(),
    `accept-token returned ${loginRes.status()} — cannot test refresh`,
  ).toBe(true);

  // Now call refresh — the context has both AT and RT cookies from accept-token
  const res = await ctx.post("/api/auth/refresh");
  expect(
    res.ok(),
    `POST /api/auth/refresh returned ${res.status()} — refresh token rotation may be broken`,
  ).toBe(true);

  const body = await res.json();
  expect(body.expires_in, "Expected expires_in in refresh response").toBe(600);

  await ctx.dispose();
});
