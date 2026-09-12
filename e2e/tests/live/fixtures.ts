import { randomUUID } from "node:crypto";
import {
  test as base,
  expect,
  type APIRequestContext,
  type BrowserContext,
  type StorageState,
} from "@playwright/test";
import { ingestStorageV2Session } from "../storage-v2-fixtures";

type RequestFactory = {
  newContext: (options?: {
    baseURL?: string;
    timeout?: number;
  }) => Promise<APIRequestContext>;
};

export type HostedQaTranscript = {
  sessionId: string;
  project: string;
  userText: string;
  assistantText: string;
  searchText: string;
};

export type HostedQaCohortSession = HostedQaTranscript & {
  label: string;
  startedAt: string;
  endedAt: string | null;
  events: Array<{
    role: string;
    content_text: string;
    timestamp: string;
  }>;
};

export type HostedQaCohort = {
  project: string;
  sessions: {
    active_recent: HostedQaCohortSession;
    recent_closed: HostedQaCohortSession;
    cold_gt_30d: HostedQaCohortSession;
    older_projection: HostedQaCohortSession;
    random_readable: HostedQaCohortSession;
  };
  ownedSessionIds: string[];
  lexicalQuery: string;
  recallQuery: string;
};

type HostedQaCleanupState = HostedQaTranscript & {
  attempted: boolean;
  materialized: boolean;
};

export function isIgnorablePlaywrightArtifactError(error: unknown): boolean {
  return (
    error instanceof Error &&
    error.message.includes("ENOENT") &&
    error.message.includes(".playwright-artifacts")
  );
}

/**
 * Wait for the hot API lane to be ready before running tests.
 * Typed archive-only degradation is acceptable when /api/readyz confirms it.
 * This prevents flaky tests during deploy windows.
 */
export async function waitForHealthy(
  requestFactory: RequestFactory,
  apiBaseUrl: string,
  options: {
    timeoutMs?: number;
    intervalMs?: number;
    requiredConsecutive?: number;
  } = {},
): Promise<void> {
  const {
    timeoutMs = 30_000,
    intervalMs = 2_000,
    requiredConsecutive = 2,
  } = options;
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
        const response = await healthRequest.get("/api/health");
        if (response.ok()) {
          const data = await response.json();
          let ready = data.status === "healthy" || data.status === "ok";
          if (data.status === "degraded") {
            const readyResponse = await healthRequest.get("/api/readyz");
            if (readyResponse.ok()) {
              const readyData = await readyResponse.json();
              ready = readyData.status === "ready_with_archive_degraded";
            }
          }
          if (ready) {
            consecutiveOk++;
            if (consecutiveOk >= requiredConsecutive) {
              console.log(
                `[health] Ready after ${attempt} attempts (${Date.now() - startTime}ms)`,
              );
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

    throw new Error(
      `[health] Readiness did not converge after ${attempt} attempts`,
    );
  } finally {
    await healthRequest.dispose().catch(() => {});
  }
}

export function normalizeToken(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  if (
    (trimmed.startsWith("'") && trimmed.endsWith("'")) ||
    (trimmed.startsWith('"') && trimmed.endsWith('"'))
  ) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

export function readDeviceToken(): string {
  return normalizeToken(process.env.LONGHOUSE_DEVICE_TOKEN) ?? "";
}

export function buildRuntimeTokenStorageState(
  baseUrl: string,
  runtimeToken: string,
): StorageState {
  const parsed = new URL(baseUrl);
  const secure = parsed.protocol === "https:";
  const cookie = secure
    ? {
        name: "__Host-lh_session",
        value: runtimeToken,
        domain: parsed.hostname,
        path: "/",
        expires: Math.floor(Date.now() / 1000) + 3600,
        httpOnly: true,
        secure: true,
        sameSite: "Lax" as const,
      }
    : {
        name: "longhouse_session",
        value: runtimeToken,
        domain: parsed.hostname,
        path: "/",
        expires: Math.floor(Date.now() / 1000) + 3600,
        httpOnly: true,
        secure: false,
        sameSite: "Lax" as const,
      };
  return {
    cookies: [cookie],
    origins: [],
  };
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
  hostedQaTranscript: HostedQaTranscript;
  hostedQaCohort: HostedQaCohort;
};

async function waitForHostedQaTranscript(
  request: APIRequestContext,
  fixture: HostedQaTranscript,
): Promise<void> {
  await expect
    .poll(
      async () => {
        return hostedQaSessionPresent(request, fixture);
      },
      { timeout: 15_000, intervals: [500, 1_000, 2_000] },
    )
    .toBe(true);
}

async function waitForHostedQaTranscripts(
  request: APIRequestContext,
  fixtures: HostedQaTranscript[],
): Promise<void> {
  await expect
    .poll(
      async () => {
        const present = await Promise.all(
          fixtures.map((fixture) => hostedQaSessionPresent(request, fixture)),
        );
        return present.every(Boolean);
      },
      { timeout: 15_000, intervals: [500, 1_000, 2_000] },
    )
    .toBe(true);
}

async function hostedQaSessionPresent(
  request: APIRequestContext,
  fixture: HostedQaTranscript,
): Promise<boolean> {
  const limit = 100;
  let offset = 0;
  let total: number | undefined;

  while (true) {
    const params = new URLSearchParams({
      provider: "claude",
      include_test: "true",
      include_automation: "true",
      hide_autonomous: "false",
      project: fixture.project,
      days_back: "90",
      limit: String(limit),
      offset: String(offset),
    });
    const response = await request.get(`/api/agents/sessions?${params}`);
    if (!response.ok()) {
      throw new Error(
        `Hosted QA served inventory returned ${response.status()}: ${await response.text()}`,
      );
    }
    const body = await response.json();
    const sessions = Array.isArray(body?.sessions) ? body.sessions : null;
    if (!sessions) {
      throw new Error("Hosted QA served inventory has no valid sessions list");
    }
    const inventoryTotal = body?.total;
    if (
      !Number.isInteger(inventoryTotal) ||
      inventoryTotal < 0 ||
      (total !== undefined && inventoryTotal !== total)
    ) {
      throw new Error(
        "Hosted QA served inventory has an invalid or changing total",
      );
    }
    total ??= inventoryTotal;
    if (
      sessions.some(
        (session: { id?: unknown }) => session.id === fixture.sessionId,
      )
    ) {
      return true;
    }
    offset += sessions.length;
    if (offset >= total) return false;
    if (sessions.length === 0) {
      throw new Error("Hosted QA served inventory pagination ended early");
    }
  }
}

async function waitForHostedQaSessionGone(
  request: APIRequestContext,
  fixture: HostedQaTranscript,
): Promise<void> {
  await expect
    .poll(() => hostedQaSessionPresent(request, fixture), {
      timeout: 15_000,
      intervals: [500, 1_000, 2_000],
    })
    .toBe(false);
}

async function cleanupHostedQaSession(
  request: APIRequestContext,
  state: HostedQaCleanupState,
): Promise<void> {
  if (!state.attempted) return;

  const visibilityResponse = await request.patch(
    `/api/agents/sessions/${state.sessionId}/timeline-visibility`,
    { data: { hidden: true } },
  );
  if (visibilityResponse.status() === 404) {
    const present = await hostedQaSessionPresent(request, state);
    if (state.materialized || present) {
      throw new Error(
        `Hosted QA fixture ${state.sessionId} was absent before retirement but was materialized or served (materialized=${state.materialized}, present=${present})`,
      );
    }
    return;
  }
  if (!visibilityResponse.ok()) {
    throw new Error(
      `Hosted QA fixture visibility retirement failed: ${visibilityResponse.status()} ${await visibilityResponse.text()}`,
    );
  }

  const visibility = await visibilityResponse.json();
  if (visibility.hidden !== true) {
    throw new Error(
      `Hosted QA fixture visibility retirement was not confirmed: ${JSON.stringify(visibility)}`,
    );
  }

  const archiveResponse = await request.post(
    `/api/agents/sessions/${state.sessionId}/action`,
    { data: { action: "archive" } },
  );
  if (!archiveResponse.ok()) {
    throw new Error(
      `Hosted QA fixture archive retirement failed: ${archiveResponse.status()} ${await archiveResponse.text()}`,
    );
  }

  const archive = await archiveResponse.json();
  if (archive.user_state !== "archived") {
    throw new Error(
      `Hosted QA fixture archive retirement was not confirmed: ${JSON.stringify(archive)}`,
    );
  }
  await waitForHostedQaSessionGone(request, state);
}

async function cleanupHostedQaSessions(
  request: APIRequestContext,
  states: HostedQaCleanupState[],
): Promise<void> {
  const failures: unknown[] = [];
  for (const state of [...states].reverse()) {
    try {
      await cleanupHostedQaSession(request, state);
    } catch (error) {
      failures.push(error);
    }
  }
  if (failures.length > 0) {
    throw new AggregateError(failures, "Hosted QA fixture cleanup failed");
  }
}

function buildHostedQaCohortSession(
  project: string,
  label: string,
  marker: string,
  ageDays: number,
  ended: boolean,
  eventCount: number,
): HostedQaCohortSession {
  const sessionId = randomUUID();
  const startedMs = Date.now() - ageDays * 24 * 60 * 60 * 1000;
  const events = Array.from({ length: eventCount }, (_, index) => {
    const role = index % 2 === 0 ? "user" : "assistant";
    return {
      role,
      content_text: `${marker}-${label}-${role}-${index}`,
      timestamp: new Date(startedMs + index * 1_000).toISOString(),
    };
  });
  const startedAt = events[0].timestamp;
  const endedAt = ended ? events.at(-1)!.timestamp : null;
  const userText = events.find((event) => event.role === "user")!.content_text;
  const assistantText =
    events.find((event) => event.role === "assistant")?.content_text ??
    userText;
  return {
    sessionId,
    project,
    label,
    startedAt,
    endedAt,
    events,
    userText,
    assistantText,
    searchText: assistantText,
  };
}

export const test = base.extend<LiveFixtures>({
  apiBaseUrl: [
    async ({}, use) => {
      const apiBaseUrl =
        process.env.API_URL ||
        process.env.PLAYWRIGHT_API_BASE_URL ||
        process.env.E2E_API_URL ||
        "";
      await use(apiBaseUrl);
    },
    { scope: "worker" },
  ],

  frontendBaseUrl: [
    async ({ apiBaseUrl }, use) => {
      const frontendBaseUrl =
        process.env.FRONTEND_URL ||
        process.env.PLAYWRIGHT_BASE_URL ||
        process.env.E2E_FRONTEND_URL ||
        apiBaseUrl;
      await use(frontendBaseUrl);
    },
    { scope: "worker" },
  ],

  browserStorageState: [
    async ({ apiBaseUrl, playwright }, use) => {
      const runtimeToken = normalizeToken(process.env.SMOKE_RUNTIME_TOKEN);
      if (runtimeToken) {
        await waitForHealthy(playwright.request, apiBaseUrl);
        await use(buildRuntimeTokenStorageState(apiBaseUrl, runtimeToken));
        return;
      }

      throw new Error("The hosted QA runner must supply SMOKE_RUNTIME_TOKEN");
    },
    { scope: "worker" },
  ],

  authToken: [
    async ({ apiBaseUrl, playwright }, use) => {
      if (!process.env.RUN_LIVE_E2E) {
        test.skip(true, "RUN_LIVE_E2E not set; skipping live prod E2E");
      }

      if (!apiBaseUrl) {
        test.skip(
          true,
          "API_URL or PLAYWRIGHT_API_BASE_URL required; skipping live prod E2E",
        );
      }

      const runtimeToken = normalizeToken(process.env.SMOKE_RUNTIME_TOKEN);
      if (runtimeToken) {
        await waitForHealthy(playwright.request, apiBaseUrl);
        await use(runtimeToken);
        return;
      }

      throw new Error("The hosted QA runner must supply SMOKE_RUNTIME_TOKEN");
    },
    { scope: "worker" },
  ],

  deviceToken: [
    async ({}, use) => {
      await use(readDeviceToken());
    },
    { scope: "worker" },
  ],

  request: async ({ playwright, apiBaseUrl, authToken }, use) => {
    const request = await playwright.request.newContext({
      baseURL: apiBaseUrl,
      extraHTTPHeaders: {
        Authorization: `Bearer ${authToken}`,
      },
      timeout: 45_000,
    });
    await use(request);
    await request.dispose().catch((error) => {
      if (!isIgnorablePlaywrightArtifactError(error)) {
        throw error;
      }
    });
  },

  agentsRequest: async ({ playwright, apiBaseUrl, deviceToken }, use) => {
    // `/api/agents/*` uses the explicit device-token header. The browser fixture
    // may use that same owner-bound token as its session cookie because hosted
    // auth resolves `zdt_` principals through the canonical device-token store.
    const extraHTTPHeaders: Record<string, string> = {};
    if (deviceToken) {
      extraHTTPHeaders["X-Agents-Token"] = deviceToken;
    }

    const request = await playwright.request.newContext({
      baseURL: apiBaseUrl,
      extraHTTPHeaders,
      timeout: 45_000,
    });
    await use(request);
    await request.dispose().catch((error) => {
      if (!isIgnorablePlaywrightArtifactError(error)) {
        throw error;
      }
    });
  },

  context: async ({ browser, frontendBaseUrl, browserStorageState }, use) => {
    const context = await browser.newContext({
      baseURL: frontendBaseUrl,
      storageState: browserStorageState,
    });

    try {
      await use(context);
    } finally {
      await context.close().catch((error) => {
        if (!isIgnorablePlaywrightArtifactError(error)) {
          throw error;
        }
      });
    }
  },
  hostedQaTranscript: async ({ agentsRequest }, use) => {
    const sessionId = randomUUID();
    const marker = `hosted-qa-${sessionId}`;
    const project = `hosted-qa-${sessionId.slice(0, 8)}`;
    const userText = `Owned hosted QA user transcript ${marker}`;
    const assistantText = `Owned hosted QA assistant transcript ${marker}`;
    const startedAt = new Date(Date.now() - 60_000).toISOString();
    const endedAt = new Date().toISOString();
    const fixture: HostedQaTranscript = {
      sessionId,
      project,
      userText,
      assistantText,
      searchText: assistantText,
    };
    const state: HostedQaCleanupState = {
      ...fixture,
      attempted: false,
      materialized: false,
    };

    try {
      state.attempted = true;
      await ingestStorageV2Session(agentsRequest, {
        sessionId,
        provider: "claude",
        environment: "test",
        project,
        cwd: "/tmp/longhouse-hosted-qa",
        providerSessionId: `hosted-qa-${sessionId}`,
        startedAt,
        endedAt,
        originKind: "test_or_canary",
        hiddenFromDefaultTimeline: true,
        launchActor: "automation",
        launchSurface: "test",
        events: [
          { role: "user", content_text: userText, timestamp: startedAt },
          {
            role: "assistant",
            content_text: assistantText,
            timestamp: endedAt,
          },
        ],
      });
      state.materialized = true;
      await waitForHostedQaTranscript(agentsRequest, fixture);
      await use(fixture);
    } finally {
      await cleanupHostedQaSession(agentsRequest, state);
    }
  },
  hostedQaCohort: async ({ agentsRequest }, use) => {
    const marker = `hosted-qa-cohort-${randomUUID()}`;
    const project = `${marker}-project`;
    const sessions = {
      active_recent: buildHostedQaCohortSession(
        project,
        "active-recent",
        marker,
        1,
        false,
        2,
      ),
      recent_closed: buildHostedQaCohortSession(
        project,
        "recent-closed",
        marker,
        2,
        true,
        2,
      ),
      cold_gt_30d: buildHostedQaCohortSession(
        project,
        "cold-31-90d",
        marker,
        45,
        true,
        2,
      ),
      older_projection: buildHostedQaCohortSession(
        project,
        "older-pagination",
        marker,
        45,
        true,
        205,
      ),
      random_readable: buildHostedQaCohortSession(
        project,
        "independent-readable",
        marker,
        10,
        true,
        2,
      ),
    };
    const cohort: HostedQaCohort = {
      project,
      sessions,
      ownedSessionIds: Object.values(sessions).map(
        (session) => session.sessionId,
      ),
      lexicalQuery: sessions.active_recent.searchText,
      recallQuery: sessions.cold_gt_30d.searchText,
    };
    const states: HostedQaCleanupState[] = Object.values(sessions).map(
      (session) => ({
        ...session,
        attempted: false,
        materialized: false,
      }),
    );

    try {
      for (const [index, session] of Object.values(sessions).entries()) {
        const state = states[index];
        state.attempted = true;
        await ingestStorageV2Session(agentsRequest, {
          sessionId: session.sessionId,
          provider: "claude",
          environment: "test",
          project: session.project,
          cwd: "/tmp/longhouse-hosted-qa",
          providerSessionId: `${project}-${session.sessionId}`,
          startedAt: session.startedAt,
          endedAt: session.endedAt,
          originKind: "test_or_canary",
          hiddenFromDefaultTimeline: true,
          launchActor: "automation",
          launchSurface: "test",
          events: session.events,
        });
        state.materialized = true;
      }
      await waitForHostedQaTranscripts(agentsRequest, Object.values(sessions));
      await use(cohort);
    } finally {
      await cleanupHostedQaSessions(agentsRequest, states);
    }
  },
});

export { expect } from "@playwright/test";
