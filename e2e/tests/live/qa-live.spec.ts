/**
 * Live QA harness for the Longhouse production instance.
 *
 * Designed to run after every deploy — headless, ~60s, exit 0=pass exit 1=fail.
 * Uses the hosted runtime-token/device-token flow shared by the other live suites.
 *
 * Run via: ./scripts/qa-live.sh
 * Or:      make qa-live
 */

import {
  test,
  expect,
  isIgnorablePlaywrightArtifactError,
  normalizeToken,
  buildRuntimeTokenStorageState,
} from "./fixtures";
import type { Page } from "@playwright/test";
import { waitForPageReady } from "../helpers/ready-signals";

// ---------------------------------------------------------------------------
// Shared error collectors
// ---------------------------------------------------------------------------

// Known benign console noise to suppress (browser extensions, HMR, etc.)
const BENIGN_CONSOLE_PATTERNS = [
  /Download the React DevTools/,
  /\[HMR\]/,
  /Failed to load resource.*favicon/i,
  // Response collection below records owned frontend/API failures with the
  // exact URL and status. Chromium's generic console message omits the URL,
  // so treating it as authoritative makes a third-party font CDN outage look
  // like a Longhouse 500.
  /^Failed to load resource: the server responded with a status of \d+/i,
  /Content Security Policy/,
];

/** Attach console error + 4xx/5xx response collectors to a page. */
function attachErrorCollectors(
  page: Page,
  frontendBaseUrl: string,
  apiBaseUrl: string,
): {
  consoleErrors: string[];
  serverErrors: string[];
} {
  const consoleErrors: string[] = [];
  const serverErrors: string[] = [];
  const ownedOrigins = new Set(
    [frontendBaseUrl, apiBaseUrl]
      .filter(Boolean)
      .map((url) => new URL(url).origin),
  );

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
    const owned = ownedOrigins.has(new URL(url).origin);
    // Catch all owned 5xx responses, including static assets, and owned API
    // 4xx responses. Authentication checks retain their more specific errors.
    if (
      owned &&
      (status >= 500 ||
        (url.includes("/api/") && status >= 400 && status !== 401))
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

async function scopeTimelineToOwnedProject(
  page: Page,
  project: string,
): Promise<void> {
  await page.route("**/api/timeline/sessions**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/timeline/sessions") {
      await route.continue();
      return;
    }
    url.searchParams.set("project", project);
    url.searchParams.set("include_test", "true");
    url.searchParams.set("include_automation", "true");
    const response = await route.fetch({ url: url.toString() });
    await route.fulfill({ response });
  });
  await page.route("**/api/timeline/recall**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/timeline/recall") {
      await route.continue();
      return;
    }
    url.searchParams.set("project", project);
    url.searchParams.set("include_test", "true");
    url.searchParams.set("include_automation", "true");
    const response = await route.fetch({ url: url.toString() });
    await route.fulfill({ response });
  });
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

// ---------------------------------------------------------------------------
// Test 1: Auth + Timeline loads
// ---------------------------------------------------------------------------

test("auth + timeline loads with session rows or an empty state", async ({
  context,
  frontendBaseUrl,
  apiBaseUrl,
}) => {
  test.setTimeout(20_000);

  const page = await context.newPage();
  const { consoleErrors, serverErrors } = attachErrorCollectors(
    page,
    frontendBaseUrl,
    apiBaseUrl,
  );

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

    if (
      authFailed ||
      serverErrors.length > 0 ||
      consoleErrors.length > 0 ||
      attempt === 2
    ) {
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
      "Auth failure: /api/timeline/sessions returned 401. Check SMOKE_RUNTIME_TOKEN.",
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

  const rowCount = await page.getByTestId("session-row").count();
  // A freshly reprovisioned canary is allowed to have no transcript rows. The
  // rendered empty state proves auth, data readiness, and the timeline shell
  // without coupling deploy QA to retained tenant data.
  const emptyStateVisible = await page
    .locator(".sessions-hero-empty")
    .isVisible()
    .catch(() => false);
  expect(
    rowCount > 0 || emptyStateVisible,
    `Expected session rows or the guided empty state on /timeline, found ${rowCount} rows.`,
  ).toBe(true);

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 2: Removed routes resolve to timeline
// ---------------------------------------------------------------------------

test("removed route auth fallback resolves to timeline", async ({
  browser,
  frontendBaseUrl,
}) => {
  test.setTimeout(30_000);

  const runtimeToken = normalizeToken(process.env.SMOKE_RUNTIME_TOKEN);
  if (!runtimeToken) {
    test.skip(true, "SMOKE_RUNTIME_TOKEN not set");
    return;
  }
  const baseOrigin = new URL(frontendBaseUrl).origin;
  const context = await browser.newContext({ baseURL: baseOrigin });
  const page = await context.newPage();

  try {
    // /loop is a removed SPA route. The supported unauthenticated contract is
    // the configured tenant login handoff, not a hard-coded control hostname.
    const methodsResponse = await context.request.get(
      `${baseOrigin}/api/auth/methods`,
    );
    expect(
      methodsResponse.ok(),
      `GET /api/auth/methods returned ${methodsResponse.status()}`,
    ).toBe(true);
    const methods = await methodsResponse.json();
    const configuredLoginUrl =
      typeof methods?.sso_login_url === "string"
        ? new URL(methods.sso_login_url)
        : null;

    if (methods?.sso === true) {
      expect(
        configuredLoginUrl,
        "SSO-enabled tenants must publish their configured login URL",
      ).not.toBeNull();
    }

    await page.goto(`${baseOrigin}/loop`, { waitUntil: "domcontentloaded" });

    if (methods?.sso === true) {
      await expect(page).toHaveURL(
        (url) =>
          url.origin === configuredLoginUrl?.origin &&
          url.pathname === configuredLoginUrl?.pathname,
        { timeout: 15_000 },
      );
    } else {
      await expect(page).toHaveURL(
        (url) => url.origin === baseOrigin && url.pathname === "/login",
        { timeout: 15_000 },
      );
    }

    // --- Part 2: authenticated removed /loop route lands on the supported home route ---
    const state = buildRuntimeTokenStorageState(baseOrigin, runtimeToken);
    await context.addCookies(state.cookies);
    await page.goto(`${baseOrigin}/loop`, { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(
      (url) => url.origin === baseOrigin && url.pathname === "/timeline",
      { timeout: 20_000 },
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
  frontendBaseUrl,
  apiBaseUrl,
}) => {
  // Budget includes auth checks + redirect + timeline render.
  test.setTimeout(45_000);

  const page = await context.newPage();
  const { consoleErrors, serverErrors } = attachErrorCollectors(
    page,
    frontendBaseUrl,
    apiBaseUrl,
  );

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
    .locator(
      '.sessions-page, .sessions-hero-empty, [data-testid="session-row"]',
    )
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

test("owned canary transcript detail renders exact events", async ({
  context,
  agentsRequest,
  frontendBaseUrl,
  apiBaseUrl,
  hostedQaTranscript,
}) => {
  test.setTimeout(45_000);

  const candidateSessionIds = [hostedQaTranscript.sessionId];
  const eventsResponse = await agentsRequest.get(
    `/api/agents/sessions/${hostedQaTranscript.sessionId}/events?limit=20`,
  );
  expect(
    eventsResponse.ok(),
    `Owned fixture events returned ${eventsResponse.status()}: ${await eventsResponse.text()}`,
  ).toBe(true);
  const eventsBody = await eventsResponse.json();
  const eventTexts = (
    Array.isArray(eventsBody?.events) ? eventsBody.events : []
  )
    .map((event: { content_text?: unknown }) => event.content_text)
    .filter((text: unknown): text is string => typeof text === "string");
  expect(eventTexts).toEqual(
    expect.arrayContaining([
      hostedQaTranscript.userText,
      hostedQaTranscript.assistantText,
    ]),
  );

  const page = await context.newPage();

  const { consoleErrors, serverErrors } = attachErrorCollectors(
    page,
    frontendBaseUrl,
    apiBaseUrl,
  );
  const authErrors: string[] = [];
  let detailPath = "";
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

  const emptySessionIds: string[] = [];
  let renderedSessionId: string | null = null;

  for (const sessionId of candidateSessionIds) {
    authErrors.length = 0;
    detailPath = `/api/timeline/sessions/${sessionId}`;

    await page.goto(`/timeline/${sessionId}`, {
      waitUntil: "domcontentloaded",
    });

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

    const renderedTimeline = await timelineItems
      .first()
      .waitFor({ timeout: 4_000 })
      .then(() => true)
      .catch(() => false);
    if (renderedTimeline) {
      renderedSessionId = sessionId;
      break;
    }
    emptySessionIds.push(sessionId);
  }

  if (!renderedSessionId) {
    await failWithScreenshot(
      page,
      "session-detail",
      `No compatible timeline items found in ${candidateSessionIds.length} candidate sessions. Empty detail candidates: ${emptySessionIds.join(", ")}. Expected [data-testid=\"session-timeline-row\"], button[id^=\"event-\"], .timeline-row, or .event-item.`,
    );
  }

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
    `Expected at least 1 compatible timeline item in session ${renderedSessionId}`,
  ).toBeGreaterThan(0);
  await expect(
    page
      .getByTestId("session-timeline-list")
      .getByText(hostedQaTranscript.userText, { exact: true }),
  ).toBeVisible();
  await expect(
    page
      .getByTestId("session-timeline-list")
      .getByText(hostedQaTranscript.assistantText, { exact: true }),
  ).toBeVisible();

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 4: Health + API sanity
// ---------------------------------------------------------------------------

test("health endpoint confirms the hot lane is ready", async ({
  agentsRequest,
}) => {
  test.setTimeout(10_000);

  const res = await agentsRequest.get("/api/health");
  expect(res.ok(), `GET /api/health returned ${res.status()}`).toBe(true);

  const body = await res.json();
  if (body.status === "degraded") {
    const ready = await agentsRequest.get("/api/readyz");
    expect(ready.ok(), `GET /api/readyz returned ${ready.status()}`).toBe(true);
    const readiness = await ready.json();
    expect(readiness.status).toBe("ready_with_archive_degraded");
  } else {
    expect(
      body.status,
      `Expected health.status to be "healthy" or "ok", got: ${body.status}`,
    ).toMatch(/^(healthy|ok)$/);
  }
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

test("owned closed-session projection never exposes live composer", async ({
  context,
  hostedQaCohort,
}) => {
  test.setTimeout(20_000);

  const sessionId = hostedQaCohort.sessions.recent_closed.sessionId;

  const workspaceResponse = await context.request.get(
    `/api/timeline/sessions/${sessionId}/workspace?limit=5`,
  );
  expect(
    workspaceResponse.ok(),
    `GET /api/timeline/sessions/${sessionId}/workspace returned ${workspaceResponse.status()}`,
  ).toBe(true);

  const workspace = await workspaceResponse.json();
  const session = workspace?.session;
  expect(session?.runtime_display?.lifecycle).toBe("closed");
  expect(session?.capabilities?.live_control_available).toBe(false);
  expect(session?.capabilities?.reply_to_live_session_available).toBe(false);
  expect(session?.capabilities?.can_queue_next_input).toBe(false);
  expect(session?.capabilities?.can_steer_active_turn).toBe(false);
  expect(session?.capabilities?.host_reattach_available).toBe(false);
  expect(session?.capabilities?.composer_enabled).toBe(false);
  expect(session?.capabilities?.attach_images ?? false).toBe(false);
});

// ---------------------------------------------------------------------------
// Launch picker: workspace suggestions via the browser-cookie surface
//
// The iOS launch sheet and web LaunchSessionModal both read workspace
// suggestions through the cookie-authed /api/timeline/machines/{id}/workspaces
// endpoint. The /api/agents/* sibling is device-token-only and 401s for those
// clients. This test exercises the SAME auth path the apps use, so an endpoint
// or auth-surface regression fails the deploy instead of landing on a human.
// ---------------------------------------------------------------------------

test("launch picker: workspaces load via browser-cookie surface", async ({
  context,
}) => {
  test.setTimeout(20_000);

  // Discover an enrolled machine through the same cookie-authed directory the
  // launch sheet uses.
  const machinesResponse = await context.request.get("/api/timeline/machines");
  expect(
    machinesResponse.ok(),
    `GET /api/timeline/machines returned ${machinesResponse.status()} — browser auth may be broken`,
  ).toBe(true);
  const machines = (await machinesResponse.json())?.machines ?? [];
  expect(Array.isArray(machines), "machines should be an array").toBe(true);

  if (machines.length === 0) {
    test.skip(true, "No enrolled machines on this instance");
    return;
  }

  const deviceId: string = machines[0].device_id;
  const workspacesResponse = await context.request.get(
    `/api/timeline/machines/${encodeURIComponent(deviceId)}/workspaces?limit=12`,
  );
  expect(
    workspacesResponse.ok(),
    `GET /api/timeline/machines/${deviceId}/workspaces returned ${workspacesResponse.status()} ` +
      `— launch picker is broken for the browser/iOS auth surface`,
  ).toBe(true);

  const body = await workspacesResponse.json();
  expect(body.device_id, "response echoes the requested device_id").toBe(
    deviceId,
  );
  expect(Array.isArray(body.workspaces), "workspaces should be an array").toBe(
    true,
  );

  // Shape + frecency contract: scores are present and descending, paths are
  // absolute, and suggestions are scoped to the requested device (no env leak).
  let previousScore = Infinity;
  for (const workspace of body.workspaces) {
    expect(typeof workspace.path).toBe("string");
    expect(
      workspace.path.startsWith("/"),
      `workspace path should be absolute: ${workspace.path}`,
    ).toBe(true);
    expect(typeof workspace.label).toBe("string");
    expect(typeof workspace.score).toBe("number");
    expect(typeof workspace.session_count).toBe("number");
    expect(
      workspace.score <= previousScore,
      "workspaces must be ranked by descending score",
    ).toBe(true);
    previousScore = workspace.score;
  }
});

// ---------------------------------------------------------------------------
// Test 6: AI search toggle — off by default, toggles on
// ---------------------------------------------------------------------------

test("timeline search finds the owned fixture and has AI toggle", async ({
  context,
  agentsRequest,
  hostedQaTranscript,
  frontendBaseUrl,
  apiBaseUrl,
}) => {
  test.setTimeout(20_000);

  const page = await context.newPage();
  const { consoleErrors, serverErrors } = attachErrorCollectors(
    page,
    frontendBaseUrl,
    apiBaseUrl,
  );
  const authErrors: string[] = [];
  page.on("response", (response) => {
    if (response.url().includes("/api/") && response.status() === 401) {
      authErrors.push(response.url());
    }
  });
  await scopeTimelineToOwnedProject(page, hostedQaTranscript.project);

  await expect
    .poll(
      async () => {
        const params = new URLSearchParams({
          include_test: "true",
          include_automation: "true",
          query: hostedQaTranscript.searchText,
          limit: "10",
        });
        const response = await agentsRequest.get(
          `/api/agents/sessions?${params}`,
        );
        if (!response.ok()) return false;
        const body = await response.json();
        const sessions = Array.isArray(body?.sessions) ? body.sessions : [];
        return sessions.some(
          (session: { id?: unknown }) =>
            session.id === hostedQaTranscript.sessionId,
        );
      },
      { timeout: 20_000, intervals: [500, 1_000, 2_000] },
    )
    .toBe(true);

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

  await page
    .getByPlaceholder("Search sessions...")
    .fill(hostedQaTranscript.searchText);
  const ownedRow = page.getByTestId("session-row").first();
  await expect(ownedRow).toBeVisible({
    timeout: 15_000,
  });
  const visibleIds = await page
    .getByTestId("session-row")
    .evaluateAll((rows) =>
      rows
        .map((row) => row.getAttribute("data-session-id"))
        .filter((id): id is string => Boolean(id)),
    );
  expect(visibleIds).toEqual([hostedQaTranscript.sessionId]);

  if (authErrors.length > 0) {
    await failWithScreenshot(
      page,
      "timeline-search-auth",
      `Auth failures while searching: ${authErrors.join(", ")}`,
    );
  }
  if (serverErrors.length > 0) {
    await failWithScreenshot(
      page,
      "timeline-search-500",
      `Server errors while searching: ${serverErrors.join(", ")}`,
    );
  }
  if (consoleErrors.length > 0) {
    await failWithScreenshot(
      page,
      "timeline-search-console",
      `JS errors while searching: ${consoleErrors.join(" | ")}`,
    );
  }

  await page.close();
});

// ---------------------------------------------------------------------------
// Test 7: Recall finds owned transcript content through browser authentication
// ---------------------------------------------------------------------------

test("browser recall renders the owned transcript match", async ({
  context,
  hostedQaTranscript,
}) => {
  test.setTimeout(45_000);

  const page = await context.newPage();
  await scopeTimelineToOwnedProject(page, hostedQaTranscript.project);
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
  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/api/timeline/recall" &&
      url.searchParams.get("query") === hostedQaTranscript.searchText
    );
  });
  await input.fill(hostedQaTranscript.searchText);
  const response = await responsePromise;
  expect(response.ok(), `Browser recall returned ${response.status()}`).toBe(
    true,
  );
  const ownedCard = page.getByTestId("recall-card").filter({
    has: page.locator(`a[href*="/timeline/${hostedQaTranscript.sessionId}"]`),
    hasText: hostedQaTranscript.assistantText,
  });
  await expect(ownedCard).toBeVisible({ timeout: 25_000 });

  await page.close();
});
