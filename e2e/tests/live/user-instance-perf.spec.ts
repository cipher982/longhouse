import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import type { Page, TestInfo } from "@playwright/test";
import { test, expect } from "./fixtures";
import { waitForPageReady } from "../helpers/ready-signals";

type ServerTimingMetric = {
  name: string;
  duration_ms: number;
};

type ResourceSummary = {
  name: string;
  duration_ms: number;
  transfer_size: number;
  initiator_type: string;
  server_timing: ServerTimingMetric[];
  cache_control: string | null;
};

type PhaseSummary = {
  phase: string;
  ready_ms: number;
  total_api_requests: number;
  total_api_transfer_size: number;
  slowest_api_requests: ResourceSummary[];
};

type RawResourceSummary = {
  name: string;
  duration_ms: number;
  transfer_size: number;
  initiator_type: string;
  start_time_ms: number;
};

type ResponseMetadata = {
  server_timing: ServerTimingMetric[];
  cache_control: string | null;
};

function roundMs(value: number): number {
  return Math.round(value * 10) / 10;
}

function parseServerTiming(headerValue: string | null | undefined): ServerTimingMetric[] {
  if (!headerValue) {
    return [];
  }

  return headerValue
    .split(",")
    .map((segment) => segment.trim())
    .filter(Boolean)
    .map((segment) => {
      const [namePart, ...params] = segment.split(";").map((part) => part.trim());
      const durPart = params.find((part) => part.startsWith("dur="));
      const duration = durPart ? Number(durPart.slice(4)) : NaN;
      return {
        name: namePart,
        duration_ms: Number.isFinite(duration) ? roundMs(duration) : 0,
      };
    })
    .filter((metric) => metric.name.length > 0);
}

async function clearResourceTimings(page: Page): Promise<void> {
  await page.evaluate(() => {
    performance.clearResourceTimings();
  });
}

function createApiResponseTracker(page: Page) {
  const responses = new Map<string, ResponseMetadata[]>();

  const listener = (response: Awaited<ReturnType<Page["waitForResponse"]>>) => {
    const urlString = response.url();
    if (!urlString.includes("/api/")) {
      return;
    }

    const url = new URL(urlString);
    const resourceName = `${url.pathname}${url.search}`;
    const headers = response.headers();
    const entries = responses.get(resourceName) ?? [];
    entries.push({
      server_timing: parseServerTiming(headers["server-timing"]),
      cache_control: headers["cache-control"] ?? null,
    });
    responses.set(resourceName, entries);
  };

  page.on("response", listener);

  return {
    clear() {
      responses.clear();
    },
    decorate(resources: RawResourceSummary[]): ResourceSummary[] {
      const responseQueues = new Map(
        Array.from(responses.entries()).map(([name, entries]) => [name, [...entries]]),
      );

      return resources
        .map((resource) => {
          const metadata = responseQueues.get(resource.name)?.shift() ?? null;
          return {
            name: resource.name,
            duration_ms: resource.duration_ms,
            transfer_size: resource.transfer_size,
            initiator_type: resource.initiator_type,
            server_timing: metadata?.server_timing ?? [],
            cache_control: metadata?.cache_control ?? null,
          };
        })
        .sort((a, b) => b.duration_ms - a.duration_ms);
    },
    dispose() {
      page.off("response", listener);
    },
  };
}

async function collectApiResources(page: Page): Promise<RawResourceSummary[]> {
  return page.evaluate(() => {
    return (performance.getEntriesByType("resource") as PerformanceResourceTiming[])
      .filter((entry) => entry.name.includes("/api/") && entry.duration > 0)
      .map((entry) => {
        const url = new URL(entry.name);
        return {
          name: `${url.pathname}${url.search}`,
          duration_ms: Math.round(entry.duration * 10) / 10,
          transfer_size: entry.transferSize,
          initiator_type: entry.initiatorType,
          start_time_ms: Math.round(entry.startTime * 10) / 10,
        };
      })
      .sort((a, b) => a.start_time_ms - b.start_time_ms);
  });
}

async function measurePhase(
  page: Page,
  tracker: ReturnType<typeof createApiResponseTracker>,
  phase: string,
  action: () => Promise<void>,
): Promise<PhaseSummary> {
  tracker.clear();
  await clearResourceTimings(page);
  const startedAt = performance.now();
  await action();
  const resources = tracker.decorate(await collectApiResources(page));
  return {
    phase,
    ready_ms: roundMs(performance.now() - startedAt),
    total_api_requests: resources.length,
    total_api_transfer_size: resources.reduce((sum, entry) => sum + entry.transfer_size, 0),
    slowest_api_requests: resources.slice(0, 8),
  };
}

async function waitForSessionDetail(page: Page): Promise<void> {
  const timelineItems = page.locator(
    '[data-testid="session-timeline-row"], button[id^="event-"], .timeline-row, .event-item',
  );

  await waitForPageReady(page, { timeout: 20_000 });
  await expect(timelineItems.first()).toBeVisible({ timeout: 15_000 });
}

async function writePerfReport(testInfo: TestInfo, report: unknown): Promise<void> {
  const outputPath = testInfo.outputPath("user-instance-perf.json");
  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, JSON.stringify(report, null, 2));
  await testInfo.attach("user-instance-perf", {
    path: outputPath,
    contentType: "application/json",
  });
}

test("profile hosted timeline and session detail journey", async ({ context, agentsRequest }, testInfo) => {
  test.setTimeout(90_000);

  const sessionsResponse = await agentsRequest.get("/api/agents/sessions?limit=3");
  expect(sessionsResponse.ok(), `GET /api/agents/sessions returned ${sessionsResponse.status()}`).toBe(true);

  const sessionsBody = await sessionsResponse.json();
  const sessions = Array.isArray(sessionsBody?.sessions) ? sessionsBody.sessions : [];
  const firstSessionId = sessions.find((session) => typeof session?.id === "string" && session.id.length > 0)?.id ?? null;

  if (!firstSessionId) {
    test.skip(true, "No sessions available to profile");
    return;
  }

  const page = await context.newPage();
  const tracker = createApiResponseTracker(page);

  try {
    const timelinePhase = await measurePhase(page, tracker, "timeline_initial_load", async () => {
      await page.goto("/timeline", { waitUntil: "domcontentloaded" });
      await waitForPageReady(page, { timeout: 20_000 });
      await expect(page.getByTestId("session-row").first()).toBeVisible({ timeout: 15_000 });
    });

    const selectedRow = page.getByTestId("session-row").first();
    const selectedSessionId = (await selectedRow.getAttribute("data-session-id")) || firstSessionId;

    const detailPhase = await measurePhase(page, tracker, "timeline_click_to_detail", async () => {
      await selectedRow.click();
      await page.waitForURL(new RegExp(`/timeline/${selectedSessionId}$`), { timeout: 15_000 });
      await waitForSessionDetail(page);
    });

    const directDetailPhase = await measurePhase(page, tracker, "detail_direct_reload", async () => {
      await page.goto(`/timeline/${selectedSessionId}`, { waitUntil: "domcontentloaded" });
      await waitForSessionDetail(page);
    });

    const report = {
      generated_at: new Date().toISOString(),
      run_id: process.env.E2E_RUN_ID ?? null,
      base_url: process.env.PLAYWRIGHT_BASE_URL ?? null,
      session_id: selectedSessionId,
      phases: [timelinePhase, detailPhase, directDetailPhase],
    };

    console.log("\nHosted user-instance perf summary");
    for (const phase of report.phases) {
      console.log(
        `- ${phase.phase}: ready=${phase.ready_ms}ms, api_requests=${phase.total_api_requests}, api_bytes=${phase.total_api_transfer_size}`,
      );
      for (const request of phase.slowest_api_requests.slice(0, 3)) {
        const serverTiming =
          request.server_timing.length > 0
            ? ` [server: ${request.server_timing.map((metric) => `${metric.name}=${metric.duration_ms}ms`).join(", ")}]`
            : "";
        const cacheControl = request.cache_control ? ` [cache: ${request.cache_control}]` : "";
        console.log(`  slow: ${request.duration_ms}ms ${request.name}${serverTiming}${cacheControl}`);
      }
    }

    await writePerfReport(testInfo, report);
  } finally {
    tracker.dispose();
    await page.close();
  }
});
