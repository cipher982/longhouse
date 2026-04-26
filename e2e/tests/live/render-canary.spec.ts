/**
 * Render-canary E2E: measure the last 50ms of the realtime pipeline.
 *
 * The synthetic canary (producer on cube) proves ingest → pubsub → SSE
 * works. What it does NOT prove is that a real browser EventSource
 * actually receives frames and React actually re-renders without dropping
 * them. This test closes that gap.
 *
 * Strategy:
 *   1. Open the canary session's detail page in a real Chromium.
 *   2. Patch window.EventSource in an init script so we record the wall-
 *      clock arrival time of every `workspace_changed` frame, plus the
 *      next requestAnimationFrame tick after that frame (== paint).
 *   3. Let the always-on producer feed events for ~90s.
 *   4. Extract the measurements, compute p50/p95/p99 (frame → paint).
 *   5. POST the p95 back to /api/telemetry/canary-observation so the
 *      histogram gets a hop=render sample.
 *   6. Assert p95 < 500ms (SLA target). Fail the deploy otherwise.
 *
 * Env required:
 *   - SMOKE_LOGIN_TOKEN (via the existing live fixtures harness)
 *   - LONGHOUSE_CANARY_TOKEN (shared secret for observation POST)
 *
 * Env optional:
 *   - LONGHOUSE_CANARY_SESSION_ID (if unset, the test discovers it by
 *     querying /api/agents/sessions?provider=canary)
 */

import { test, expect } from "./fixtures";

const SLA_P95_MS = parseInt(process.env.RENDER_CANARY_SLA_P95_MS || "500", 10);
const OBSERVE_WINDOW_MS = parseInt(process.env.RENDER_CANARY_WINDOW_MS || "90000", 10);
const MIN_SAMPLES = parseInt(process.env.RENDER_CANARY_MIN_SAMPLES || "2", 10);

interface FrameSample {
  frameName: string;
  arrivedAtMs: number;
  paintedAtMs: number;
  paintDeltaMs: number;
  serverNowMs?: number | null;
  latestEventEmittedAtMs?: number | null;
  pubsubSeq?: number | null;
}

function pct(values: number[], p: number): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const k = Math.max(0, Math.min(sorted.length - 1, Math.round((p / 100) * (sorted.length - 1))));
  return sorted[k];
}


test("render canary: SSE frame arrival → browser paint under SLA", async ({
  apiBaseUrl,
  frontendBaseUrl,
  browserStorageState,
  context,
}) => {
  test.setTimeout(OBSERVE_WINDOW_MS + 60_000);

  const canaryToken = process.env.LONGHOUSE_CANARY_TOKEN;
  if (!canaryToken) {
    test.skip(true, "LONGHOUSE_CANARY_TOKEN not set");
    return;
  }

  let canarySessionId = process.env.LONGHOUSE_CANARY_SESSION_ID;
  if (!canarySessionId) {
    // Ask the server which canary session to open. Gated by canary token
    // so anon callers can't enumerate.
    const lookupUrl = `${apiBaseUrl.replace(/\/$/, "")}/api/telemetry/canary-session`;
    const lookup = await fetch(lookupUrl, { headers: { "X-Canary-Token": canaryToken } });
    if (!lookup.ok) {
      throw new Error(`canary-session lookup returned ${lookup.status}`);
    }
    const body = (await lookup.json()) as { session_id?: string | null };
    if (!body.session_id) {
      throw new Error(
        "No canary session available on server. Ensure the always-on " +
          "canary producer has ingested at least one session.",
      );
    }
    canarySessionId = body.session_id;
  }
  console.log(`[render-canary] session_id=${canarySessionId}`);

  // Use a fresh context seeded with the admin storageState so the browser
  // has the longhouse_session cookie for the SSE subscription. addInitScript
  // must run before the page's own JS so the patched EventSource wins.
  await context.addInitScript(() => {
    const globalWindow = window as unknown as {
      __canaryFrames__?: Record<string, unknown>[];
      EventSource: typeof EventSource;
    };
    globalWindow.__canaryFrames__ = [];

    const OriginalEventSource = globalWindow.EventSource;
    class PatchedEventSource extends OriginalEventSource {
      constructor(url: string | URL, init?: EventSourceInit) {
        super(url, init);
        const urlStr = typeof url === "string" ? url : url.toString();
        // Only intercept workspace streams — other SSE endpoints are noise.
        if (!urlStr.includes("/workspace/stream")) {
          return;
        }

        this.addEventListener("workspace_changed", (evt: MessageEvent) => {
          const arrivedAt = performance.now();
          let payload: Record<string, unknown> | null = null;
          try {
            payload = JSON.parse(evt.data);
          } catch {
            payload = null;
          }
          // Schedule a paint measurement at the next rAF tick. rAF fires
          // right before the browser paints, so the delta captures React
          // processing + DOM + paint budget.
          requestAnimationFrame(() => {
            const paintedAt = performance.now();
            (globalWindow.__canaryFrames__ || []).push({
              frameName: "workspace_changed",
              arrivedAtMs: arrivedAt,
              paintedAtMs: paintedAt,
              paintDeltaMs: paintedAt - arrivedAt,
              serverNowMs: payload?.server_now_ms ?? null,
              latestEventEmittedAtMs: payload?.latest_event_emitted_at_ms ?? null,
              pubsubSeq: payload?.pubsub_seq ?? null,
            });
          });
        });
      }
    }
    globalWindow.EventSource = PatchedEventSource as unknown as typeof EventSource;
  });

  const page = await context.newPage();
  const sessionPath = `/timeline/${canarySessionId}`;

  page.on("pageerror", (err) => {
    console.warn(`[render-canary] page error: ${err.message}`);
  });

  const gotoStart = Date.now();
  await page.goto(sessionPath, { waitUntil: "domcontentloaded", timeout: 30_000 });
  console.log(`[render-canary] goto ${sessionPath} took ${Date.now() - gotoStart}ms`);

  // Wait for the page's own EventSource to subscribe. A short poll for the
  // patched array to exist is enough — if SSE never initializes we fail
  // with zero samples below.
  await page.waitForFunction(
    () => typeof (window as { __canaryFrames__?: unknown }).__canaryFrames__ !== "undefined",
    { timeout: 10_000 },
  );

  console.log(`[render-canary] collecting for ${OBSERVE_WINDOW_MS}ms`);
  await page.waitForTimeout(OBSERVE_WINDOW_MS);

  const samples = await page.evaluate(() => {
    const win = window as unknown as { __canaryFrames__?: FrameSample[] };
    return win.__canaryFrames__ ?? [];
  });

  console.log(`[render-canary] captured ${samples.length} samples`);
  samples.slice(0, 5).forEach((s, i) => {
    console.log(
      `  [${i}] arrived=${s.arrivedAtMs.toFixed(1)}ms painted=${s.paintedAtMs.toFixed(1)}ms delta=${s.paintDeltaMs.toFixed(1)}ms`,
    );
  });

  expect.soft(samples.length, "at least MIN_SAMPLES SSE frames arrived").toBeGreaterThanOrEqual(MIN_SAMPLES);

  if (samples.length === 0) {
    throw new Error(
      `No SSE workspace_changed frames arrived in ${OBSERVE_WINDOW_MS}ms. ` +
        `Check that the canary producer is running (cube systemd) and the ` +
        `session ${canarySessionId} exists on ${frontendBaseUrl}.`,
    );
  }

  const paintDeltas = samples.map((s) => s.paintDeltaMs);
  const p50 = pct(paintDeltas, 50);
  const p95 = pct(paintDeltas, 95);
  const p99 = pct(paintDeltas, 99);

  console.log(
    `[render-canary] paint delta p50=${p50.toFixed(1)}ms p95=${p95.toFixed(1)}ms p99=${p99.toFixed(1)}ms (n=${samples.length})`,
  );

  // POST p95 + samples back to the server for the Prometheus histogram.
  // Best-effort: don't fail the test if the observation post fails.
  try {
    const observePath = "/api/telemetry/canary-observation";
    const targetUrl = `${apiBaseUrl.replace(/\/$/, "")}${observePath}`;
    const response = await page.request.post(targetUrl, {
      headers: {
        "X-Canary-Token": canaryToken,
        "Content-Type": "application/json",
      },
      data: {
        canary_seq: samples.length,
        hop: "render",
        surface: "web",
        latency_ms: Math.round(p95),
      },
    });
    if (response.ok()) {
      console.log(`[render-canary] posted hop=render p95=${Math.round(p95)}ms observation`);
    } else {
      console.warn(`[render-canary] observation post returned ${response.status()}`);
    }
  } catch (err) {
    console.warn(`[render-canary] observation post threw: ${err}`);
  }

  expect(p95, `render paint p95 ${p95.toFixed(1)}ms exceeds SLA ${SLA_P95_MS}ms`).toBeLessThan(SLA_P95_MS);
});
