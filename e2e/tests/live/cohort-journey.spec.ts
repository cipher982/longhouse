import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import type { APIRequestContext, Page, Response } from "@playwright/test";
import { test, expect } from "./fixtures";
import { waitForPageReady } from "../helpers/ready-signals";
import {
  assertPrivacySafeArtifact,
  classifyApiResource,
  classifyJourneyFailure,
  resultCountBucket,
  selectJourneyCohorts,
  waitForElementPaint,
  type JourneySession,
} from "./cohort-journey-helpers";

type ServerTimingMetric = { name: string; duration_ms: number };
type RouteSummary = {
  route_class: string;
  request_count: number;
  transfer_bytes: number;
  max_duration_ms: number;
  status_families: Record<string, number>;
  server_timing_max_ms: Record<string, number>;
};
type PhaseResult = {
  phase: string;
  cohort: string;
  age_bucket: string;
  outcome: "pass" | "fail";
  failure_code: string | null;
  ready_ms: number | null;
  first_paint_ms: number | null;
  nonempty: boolean;
  result_count_bucket: ReturnType<typeof resultCountBucket>;
  api: RouteSummary[];
};
type ActionResult = {
  resultCount: number;
  paintMarker: string;
  paintAfterEpochMs?: number;
};
type ApiResponse = Awaited<ReturnType<APIRequestContext["get"]>>;
type CohortInventory = { sessions: JourneySession[]; complete: boolean };

const JOURNEY_COHORT = "dogfood_scheduled_v1";
const JOURNEY_OUTPUT = resolve(
  process.env.LONGHOUSE_JOURNEY_OUTPUT || "../artifacts/cohort-journey/cohort-journey.json",
);

function roundMs(value: number): number {
  return Math.round(value * 10) / 10;
}

function parseServerTiming(headerValue: string | undefined): ServerTimingMetric[] {
  if (!headerValue) return [];
  return headerValue
    .split(",")
    .map((segment) => segment.trim())
    .filter(Boolean)
    .map((segment) => {
      const [name, ...params] = segment.split(";").map((part) => part.trim());
      const duration = Number(params.find((part) => part.startsWith("dur="))?.slice(4));
      return { name, duration_ms: Number.isFinite(duration) ? roundMs(duration) : 0 };
    })
    .filter((metric) => /^[a-z][a-z0-9_]{0,39}$/i.test(metric.name));
}

function createResponseTracker(page: Page) {
  const responses = new Map<string, Array<{ status_family: string; server_timing: ServerTimingMetric[] }>>();
  const listener = (response: Response) => {
    const routeClass = classifyApiResource(response.url());
    if (!routeClass) return;
    const queue = responses.get(routeClass) ?? [];
    queue.push({
      status_family: `${Math.floor(response.status() / 100)}xx`,
      server_timing: parseServerTiming(response.headers()["server-timing"]),
    });
    responses.set(routeClass, queue);
  };
  page.on("response", listener);

  return {
    clear: () => responses.clear(),
    async summarize(): Promise<RouteSummary[]> {
      const resources = await page.evaluate(() =>
        (performance.getEntriesByType("resource") as PerformanceResourceTiming[])
          .filter((entry) => entry.name.includes("/api/") && entry.duration > 0)
          .map((entry) => ({
            name: entry.name,
            duration_ms: entry.duration,
            transfer_bytes: entry.transferSize,
          })),
      );
      const queues = new Map([...responses.entries()].map(([key, value]) => [key, [...value]]));
      const summaries = new Map<string, RouteSummary>();
      for (const resource of resources) {
        const routeClass = classifyApiResource(resource.name);
        if (!routeClass) continue;
        const metadata = queues.get(routeClass)?.shift();
        const summary = summaries.get(routeClass) ?? {
          route_class: routeClass,
          request_count: 0,
          transfer_bytes: 0,
          max_duration_ms: 0,
          status_families: {},
          server_timing_max_ms: {},
        };
        summary.request_count += 1;
        summary.transfer_bytes += Math.max(0, Math.round(resource.transfer_bytes));
        summary.max_duration_ms = Math.max(summary.max_duration_ms, roundMs(resource.duration_ms));
        const family = metadata?.status_family ?? "unknown";
        summary.status_families[family] = (summary.status_families[family] ?? 0) + 1;
        for (const metric of metadata?.server_timing ?? []) {
          summary.server_timing_max_ms[metric.name] = Math.max(
            summary.server_timing_max_ms[metric.name] ?? 0,
            metric.duration_ms,
          );
        }
        summaries.set(routeClass, summary);
      }
      return [...summaries.values()].sort((left, right) => left.route_class.localeCompare(right.route_class));
    },
    dispose: () => page.off("response", listener),
  };
}

async function measurePhase(
  page: Page,
  tracker: ReturnType<typeof createResponseTracker>,
  phase: string,
  cohort: string,
  ageBucket: string,
  action: () => Promise<ActionResult>,
): Promise<PhaseResult> {
  tracker.clear();
  await page.evaluate(() => performance.clearResourceTimings()).catch(() => {});
  const startedAt = Date.now();
  try {
    const result = await action();
    const readyMs = Date.now() - startedAt;
    const paintStartedAt = result.paintAfterEpochMs ?? startedAt;
    const paintEpochMs = await waitForElementPaint(page, result.paintMarker, paintStartedAt);
    const paintMs = paintEpochMs - paintStartedAt;
    if (result.resultCount <= 0) throw new Error("empty_result");
    return {
      phase,
      cohort,
      age_bucket: ageBucket,
      outcome: "pass",
      failure_code: null,
      ready_ms: roundMs(readyMs),
      first_paint_ms: roundMs(paintMs),
      nonempty: true,
      result_count_bucket: resultCountBucket(result.resultCount),
      api: await tracker.summarize(),
    };
  } catch (error) {
    return {
      phase,
      cohort,
      age_bucket: ageBucket,
      outcome: "fail",
      failure_code: classifyJourneyFailure(error),
      ready_ms: null,
      first_paint_ms: null,
      nonempty: false,
      result_count_bucket: "0",
      api: await tracker.summarize().catch(() => []),
    };
  }
}

function responseFailure(response: Response | Awaited<ReturnType<APIRequestContext["get"]>>): Error {
  return new Error(`http_${Math.floor(response.status() / 100)}xx`);
}

async function runAndWaitForSuccessfulResponse<T>(
  page: Page,
  routeClass: string,
  timeoutMs: number,
  action: () => Promise<T>,
): Promise<{ response: Response; actionResult: T }> {
  let lastFailure: Response | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let listener: ((response: Response) => void) | null = null;
  const cleanup = () => {
    if (timer) clearTimeout(timer);
    if (listener) page.off("response", listener);
    timer = null;
    listener = null;
  };
  const responsePromise = new Promise<Response>((resolve, reject) => {
    listener = (response: Response) => {
      if (classifyApiResource(response.url()) !== routeClass) return;
      if (!response.ok()) {
        lastFailure = response;
        return;
      }
      cleanup();
      resolve(response);
    };
    page.on("response", listener);
    timer = setTimeout(() => {
      const error = lastFailure ? responseFailure(lastFailure) : new Error("timeout");
      cleanup();
      reject(error);
    }, timeoutMs);
  });

  try {
    const [actionResult, response] = await Promise.all([action(), responsePromise]);
    return { response, actionResult };
  } finally {
    cleanup();
  }
}

async function getWithRetry(
  request: APIRequestContext,
  url: string,
  attempts = 3,
): Promise<ApiResponse> {
  let lastError: unknown = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await request.get(url);
      if (response.ok() || (response.status() !== 429 && response.status() < 500)) return response;
      if (attempt === attempts) return response;
    } catch (error) {
      lastError = error;
      if (attempt === attempts) throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, attempt * 250));
  }
  throw lastError instanceof Error ? lastError : new Error("http_retry_exhausted");
}

function flattenTimelineCards(body: unknown): JourneySession[] {
  if (!body || typeof body !== "object") return [];
  const rows = Array.isArray((body as { sessions?: unknown }).sessions)
    ? (body as { sessions: unknown[] }).sessions
    : [];
  const result: JourneySession[] = [];
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const card = row as Record<string, unknown>;
    for (const candidate of [card.detail, card.head, card.root, card]) {
      if (!candidate || typeof candidate !== "object") continue;
      const session = candidate as Record<string, unknown>;
      if (typeof session.id !== "string" || typeof session.started_at !== "string") continue;
      result.push({
        id: session.id,
        provider: typeof session.provider === "string" ? session.provider : null,
        environment: typeof session.environment === "string" ? session.environment : null,
        started_at: session.started_at,
        ended_at: typeof session.ended_at === "string" ? session.ended_at : null,
        last_activity_at: typeof session.last_activity_at === "string" ? session.last_activity_at : null,
        timeline_anchor_at:
          typeof session.timeline_anchor_at === "string"
            ? session.timeline_anchor_at
            : typeof card.timeline_anchor_at === "string"
              ? card.timeline_anchor_at
              : null,
        user_messages: typeof session.user_messages === "number" ? session.user_messages : 0,
        assistant_messages: typeof session.assistant_messages === "number" ? session.assistant_messages : 0,
        tool_calls: typeof session.tool_calls === "number" ? session.tool_calls : 0,
      });
    }
  }
  return result;
}

async function fetchCohortInventory(request: APIRequestContext, apiBaseUrl: string): Promise<CohortInventory> {
  const base = `${apiBaseUrl.replace(/\/$/, "")}/api/timeline/sessions`;
  const first = await getWithRetry(request, `${base}?days_back=90&limit=100&offset=0`);
  if (!first.ok()) throw responseFailure(first);
  const firstBody = await first.json();
  const total = Number(firstBody?.total ?? 0);
  const offsets = new Set<number>([0]);
  if (total > 100) offsets.add(Math.max(0, total - 100));
  if (total > 200) offsets.add(Math.max(0, Math.floor(total / 2) - 50));
  const sessions = flattenTimelineCards(firstBody);
  let complete = true;
  for (const offset of [...offsets].filter((value) => value > 0)) {
    const response = await getWithRetry(request, `${base}?days_back=90&limit=100&offset=${offset}`);
    if (!response.ok()) {
      complete = false;
      continue;
    }
    sessions.push(...flattenTimelineCards(await response.json()));
  }
  return { sessions, complete };
}

async function openReadableSession(page: Page, session: JourneySession | null): Promise<ActionResult> {
  if (!session) throw new Error("missing_cohort");
  await page.goto(`/timeline/${encodeURIComponent(session.id)}`, { waitUntil: "domcontentloaded" });
  await waitForPageReady(page, { timeout: 25_000 });
  const rows = page.getByTestId("session-timeline-row");
  await expect(rows.first()).toBeVisible({ timeout: 20_000 });
  return { resultCount: await rows.count(), paintMarker: "longhouse-session-timeline-row" };
}

function loadedEntryCount(text: string | null): number {
  const match = String(text ?? "").match(/^(\d+)(?:\/(\d+))? entries/);
  return match ? Number(match[1]) : 0;
}

function ageBucket(session: JourneySession | null, nowMs: number): string {
  if (!session) return "unknown";
  const anchor = Date.parse(session.timeline_anchor_at || session.last_activity_at || session.started_at);
  if (!Number.isFinite(anchor)) return "unknown";
  return nowMs - anchor > 30 * 24 * 60 * 60 * 1000 ? "cold_31_90d" : "recent_0_30d";
}

function writeArtifact(payload: unknown, fixtureValues: string[]): void {
  assertPrivacySafeArtifact(payload, fixtureValues);
  mkdirSync(dirname(JOURNEY_OUTPUT), { recursive: true });
  const temporary = `${JOURNEY_OUTPUT}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(payload)}\n`, { encoding: "utf8", mode: 0o600 });
  renameSync(temporary, JOURNEY_OUTPUT);
}

test("scheduled dogfood cohort journey", async ({ apiBaseUrl, context }, testInfo) => {
  test.setTimeout(180_000);
  const lexicalFixture = process.env.LONGHOUSE_JOURNEY_LEXICAL_QUERY?.trim() ?? "";
  const recallFixture = process.env.LONGHOUSE_JOURNEY_RECALL_QUERY?.trim() ?? "";
  const nowMs = Date.now();
  const phases: PhaseResult[] = [];
  const preflightFailures: string[] = [];
  let build: Record<string, string | boolean> | null = null;
  let page: Page | null = null;
  let tracker: ReturnType<typeof createResponseTracker> | null = null;

  try {
    const health = await getWithRetry(context.request, `${apiBaseUrl.replace(/\/$/, "")}/api/health`);
    if (!health.ok()) throw responseFailure(health);
    const healthBody = await health.json();
    const candidateBuild = healthBody?.build;
    if (
      !candidateBuild
      || typeof candidateBuild.commit !== "string"
      || typeof candidateBuild.version !== "string"
      || typeof candidateBuild.channel !== "string"
      || typeof candidateBuild.dirty !== "boolean"
    ) {
      throw new Error("build_identity");
    }
    build = {
      version: candidateBuild.version,
      commit: candidateBuild.commit,
      channel: candidateBuild.channel,
      dirty: candidateBuild.dirty,
    };

    const system = await getWithRetry(context.request, `${apiBaseUrl.replace(/\/$/, "")}/api/system/info`);
    if (!system.ok()) throw responseFailure(system);
    if ((await system.json())?.demo_mode === true) throw new Error("demo_target");
  } catch (error) {
    preflightFailures.push(classifyJourneyFailure(error));
  }

  let inventory: JourneySession[] = [];
  try {
    const inventoryResult = await fetchCohortInventory(context.request, apiBaseUrl);
    inventory = inventoryResult.sessions;
    if (!inventoryResult.complete) preflightFailures.push("inventory_incomplete");
    if (inventory.length === 0) throw new Error("missing_cohort");
  } catch (error) {
    preflightFailures.push(classifyJourneyFailure(error));
  }
  const cohorts = selectJourneyCohorts(inventory, nowMs, new Date(nowMs).toISOString().slice(0, 10));

  page = await context.newPage();
  tracker = createResponseTracker(page);
  try {
    phases.push(await measurePhase(page, tracker, "timeline_initial_load", "timeline", "all", async () => {
      await page!.goto("/timeline?days_back=90", { waitUntil: "domcontentloaded" });
      await waitForPageReady(page!, { timeout: 25_000 });
      const rows = page!.getByTestId("session-row");
      await expect(rows.first()).toBeVisible({ timeout: 20_000 });
      return { resultCount: await rows.count(), paintMarker: "longhouse-session-row" };
    }));

    for (const [phase, cohort, session] of [
      ["active_recent_session", "active_recent", cohorts.active_recent],
      ["recent_closed_session", "recent_closed", cohorts.recent_closed],
      ["cold_session", "cold_gt_30d", cohorts.cold_gt_30d],
      ["random_readable_session", "random_readable", cohorts.random_readable],
    ] as const) {
      phases.push(await measurePhase(
        page,
        tracker,
        phase,
        cohort,
        ageBucket(session, nowMs),
        () => openReadableSession(page!, session),
      ));
    }

    phases.push(await measurePhase(
      page,
      tracker,
      "older_projection_append",
      "older_projection",
      ageBucket(cohorts.older_projection, nowMs),
      async () => {
        await openReadableSession(page!, cohorts.older_projection);
        const summary = page!.getByTestId("session-timeline-summary");
        const before = loadedEntryCount(await summary.textContent());
        const sentinel = page!.getByTestId("session-timeline-load-older");
        if ((await sentinel.count()) === 0) throw new Error("missing_cohort");
        const paintAfterEpochMs = Date.now();
        await runAndWaitForSuccessfulResponse(page!, "session_projection", 25_000, () => (
          page!.getByTestId("session-timeline-list").evaluate((element) => element.scrollTo({ top: 0 }))
        ));
        await expect.poll(async () => loadedEntryCount(await summary.textContent()), { timeout: 20_000 }).toBeGreaterThan(before);
        const after = loadedEntryCount(await summary.textContent());
        if (after <= before) throw new Error("projection_not_appended");
        return {
          resultCount: after - before,
          paintMarker: "longhouse-session-timeline-row",
          paintAfterEpochMs,
        };
      },
    ));

    phases.push(await measurePhase(page, tracker, "stable_lexical_search", "stable_lexical", "all", async () => {
      if (!lexicalFixture) throw new Error("fixture_not_configured");
      const params = new URLSearchParams({ days_back: "90", query: lexicalFixture });
      const { response } = await runAndWaitForSuccessfulResponse(page!, "lexical_search", 25_000, () => (
        page!.goto(`/timeline?${params.toString()}`, { waitUntil: "domcontentloaded" })
      ));
      await waitForPageReady(page!, { timeout: 25_000 });
      const body = await response.json();
      const total = Number(body?.total ?? 0);
      if (total <= 0) throw new Error("empty_result");
      await expect(page!.getByTestId("session-row").first()).toBeVisible({ timeout: 20_000 });
      return { resultCount: total, paintMarker: "longhouse-session-row" };
    }));

    phases.push(await measurePhase(page, tracker, "stable_recall", "stable_recall", "recent_0_90d", async () => {
      if (!recallFixture) throw new Error("fixture_not_configured");
      await page!.goto("/timeline?days_back=90", { waitUntil: "domcontentloaded" });
      await waitForPageReady(page!, { timeout: 25_000 });
      await page!.getByTestId("recall-toggle").click();
      const paintAfterEpochMs = Date.now();
      const { response } = await runAndWaitForSuccessfulResponse(page!, "recall", 35_000, () => (
        page!.getByTestId("recall-search-input").fill(recallFixture)
      ));
      const body = await response.json();
      const total = Number(body?.total ?? 0);
      if (total <= 0) throw new Error("empty_result");
      await expect(page!.getByTestId("recall-card").first()).toBeVisible({ timeout: 25_000 });
      return {
        resultCount: total,
        paintMarker: "longhouse-recall-card",
        paintAfterEpochMs,
      };
    }));
  } finally {
    tracker.dispose();
    await page.close();
  }

  const failedPhases = phases.filter((phase) => phase.outcome === "fail").map((phase) => phase.phase);
  const artifact = {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    traffic_class: "synthetic",
    synthetic_cohort: JOURNEY_COHORT,
    trigger: process.env.GITHUB_EVENT_NAME === "schedule" ? "schedule" : "operator",
    build,
    outcome: preflightFailures.length === 0 && failedPhases.length === 0 ? "pass" : "fail",
    preflight_failure_codes: [...new Set(preflightFailures)].sort(),
    phases,
  };
  writeArtifact(artifact, [lexicalFixture, recallFixture]);
  await testInfo.attach("cohort-journey-safe", {
    path: JOURNEY_OUTPUT,
    contentType: "application/json",
  });

  expect(
    [...preflightFailures.map((code) => `preflight:${code}`), ...failedPhases],
    "scheduled cohort journey failures",
  ).toEqual([]);
});
