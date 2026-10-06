#!/usr/bin/env bun
/**
 * Cold-open probe: how long a first click from the Timeline takes to show a
 * session, on the production build, over a throttled link.
 *
 * Serves web/dist with `vite preview`, answers every /api call from the
 * session-prose-idle fixture (never a real Runtime Host), throttles the page
 * like a laptop reaching a hosted Runtime Host (RTT and bandwidth below), then
 * opens /timeline in a fresh profile, waits as a person would, moves to the
 * first row and clicks it. It records, from the click: the URL change, the
 * session title in the header, the first transcript row, and the full
 * transcript. A second click (Timeline -> back to the session) is the warm
 * number.
 *
 *   bunx tsx scripts/qa/cold-open-probe.ts [--runs=5] [--port=47231]
 *     [--workspace-ms=900] [--rtt-ms=120] [--down-mbps=20] [--settle-ms=1200]
 *     [--no-build] [--out=/tmp/agents/cold-open/<label>.json]
 */
import { chromium, type BrowserContext, type Page, type Route } from "playwright";
import { execSync, spawn } from "child_process";
import { existsSync, mkdirSync, writeFileSync } from "fs";
import path from "path";
import { REPO_ROOT, isServing } from "../ui/frontend";
import { buildMachinesFleetFixture } from "../ui-fixtures/machinesFleet";
import { buildRailSessionsFixture, buildSessionProseIdleFixture } from "../ui-fixtures/sessionDetailStress";

const arg = (name: string, fallback: string) =>
  process.argv.find((a) => a.startsWith(`--${name}=`))?.split("=")[1] ?? fallback;
const RUNS = Number(arg("runs", "5"));
const PORT = Number(arg("port", "47231"));
const WORKSPACE_MS = Number(arg("workspace-ms", "900"));
const RTT_MS = Number(arg("rtt-ms", "120"));
const DOWN_MBPS = Number(arg("down-mbps", "20"));
const SETTLE_MS = Number(arg("settle-ms", "1200"));
const OUT = arg("out", "");
const BUILD = !process.argv.includes("--no-build");
const TRACE = process.argv.includes("--trace-requests");
const PROFILE = arg("profile", "");
/** Screenshot of the cold open this many ms after the click (the skeleton frame). */
const SHOT = arg("shot", "");
const SHOT_AT_MS = Number(arg("shot-at-ms", "150"));
const BASE = `http://localhost:${PORT}`;
const FULL_ROWS = Number(arg("full-rows", "8"));

const fixture = buildSessionProseIdleFixture();
const SESSION_ID = fixture.session.id;
const BASE_PATH = `/api/timeline/sessions/${SESSION_ID}`;

const SHELL: Record<string, unknown> = {
  "/api/auth/status": {
    authenticated: true,
    user: { id: 1, email: "sam@example.com", display_name: "Sam Rivera", avatar_url: null, is_active: true, created_at: "2026-03-01T12:00:00Z", role: "USER" },
  },
  "/api/auth/methods": { google: false, password: false, sso: false, sso_url: null },
  "/api/health": { status: "healthy" },
  "/api/timeline/machines": { machines: buildMachinesFleetFixture().directory.machines.slice(0, 2) },
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const unmocked = new Set<string>();

async function answer(route: Route): Promise<void> {
  const url = new URL(route.request().url());
  const p = url.pathname;
  const json = async (body: unknown, delayMs = 150) => {
    await sleep(delayMs);
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  };
  if (p in SHELL) return json(SHELL[p]);
  if (p === "/api/users/me/client-presence") return route.fulfill({ status: 204, body: "" });
  if (p === "/api/timeline/sessions") return json(buildRailSessionsFixture(fixture.session), 400);
  if (p === `${BASE_PATH}/workspace`) return json(fixture.workspace, WORKSPACE_MS);
  if (p === `${BASE_PATH}/projection`) return json(fixture.projection, WORKSPACE_MS);
  if (p === BASE_PATH) return json(fixture.session);
  if (p === `${BASE_PATH}/thread`) return json(fixture.thread);
  if (p === `${BASE_PATH}/turns`) return json(fixture.turns);
  if (p === `${BASE_PATH}/subagents`) return json({ session_id: SESSION_ID, children: [] });
  if (p.endsWith("/workspace/stream") || p.endsWith("/stream")) return route.fulfill({ status: 204, body: "" });
  if (/^\/api\/timeline\/sessions\/[^/]+\/workspace$/.test(p)) {
    await sleep(WORKSPACE_MS);
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  }
  unmocked.add(`${route.request().method()} ${p}`);
  return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
}

async function throttle(context: BrowserContext, page: Page): Promise<void> {
  const cdp = await context.newCDPSession(page);
  await cdp.send("Network.enable");
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: false });
  await cdp.send("Network.emulateNetworkConditions", {
    offline: false,
    latency: RTT_MS,
    downloadThroughput: (DOWN_MBPS * 1_000_000) / 8,
    uploadThroughput: (10 * 1_000_000) / 8,
  });
}

type Marks = { url: number; header: number; firstRow: number; full: number };

/** Times are measured from the button release, the moment a person "clicked". */
async function timeOpen(page: Page, click: () => Promise<number>): Promise<Marks> {
  let t0 = Number.POSITIVE_INFINITY;
  const at = (p: Promise<unknown>) => p.then(() => Math.max(0, Date.now() - t0));
  const url = at(page.waitForURL(`**/timeline/${SESSION_ID}**`, { timeout: 30_000 }));
  // The session's title bar portals into the app bar's slot.
  const header = at(page.waitForFunction(() => {
    const slot = document.querySelector("[data-testid='header-session-slot']");
    return Boolean(slot && (slot.textContent ?? "").trim().length > 0);
  }, undefined, { timeout: 30_000, polling: "raf" }));
  // The rows wrapper always holds the trace line; a real row is a second child.
  const rowCount = (n: number) =>
    page.waitForFunction((min) => document.querySelectorAll(".timeline-pane__rows > *").length >= min, n, { timeout: 30_000, polling: "raf" });
  const firstRow = at(rowCount(2));
  const full = at(rowCount(FULL_ROWS));
  const seen: string[] = [];
  const onReq = (r: { url: () => string }) => {
    if (TRACE && Number.isFinite(t0)) seen.push(`${Date.now() - t0}ms ${new URL(r.url()).pathname}${new URL(r.url()).search}`);
  };
  page.on("request", onReq);
  t0 = await click();
  const [u, h, r, f] = await Promise.all([url, header, firstRow, full]);
  page.off("request", onReq);
  if (TRACE) {
    const tasks = await page.evaluate(() => {
      const w = window as unknown as { __longTasks: number[][] };
      const out = w.__longTasks.slice(-8);
      w.__longTasks = [];
      return out;
    });
    console.log(`  long tasks [start, ms]: ${JSON.stringify(tasks)}`);
  }
  if (TRACE) console.log(`  requests after click:\n    ${seen.join("\n    ")}`);
  return { url: u, header: h, firstRow: r, full: f };
}

async function oneRun(): Promise<{ cold: Marks; warm: Marks }> {
  // --no-webgl isolates the Timeline's WebGL hearth (headless Chromium renders
  // WebGL in software, which exaggerates its cost against a real GPU).
  const browser = await chromium.launch({ args: process.argv.includes("--no-webgl") ? ["--disable-webgl", "--disable-webgl2"] : [] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.route(`${BASE}/api/**`, answer);
    const page = await context.newPage();
    if (TRACE) {
      await page.addInitScript(() => {
        (window as unknown as { __longTasks: number[][] }).__longTasks = [];
        new PerformanceObserver((list) => {
          for (const e of list.getEntries()) (window as unknown as { __longTasks: number[][] }).__longTasks.push([Math.round(e.startTime), Math.round(e.duration)]);
        }).observe({ type: "longtask", buffered: true });
      });
    }
    await throttle(context, page);
    await page.goto(`${BASE}/timeline`, { waitUntil: "domcontentloaded" });
    const row = page.locator(`[data-testid='session-row'][data-session-id='${SESSION_ID}']`).first();
    await row.waitFor({ state: "visible", timeout: 30_000 });
    await sleep(SETTLE_MS);
    const box = await row.boundingBox();
    if (!box) throw new Error("session row has no box");
    const cx = box.x + Math.min(200, box.width / 2);
    const cy = box.y + box.height / 2;
    const cold = await timeOpen(page, async () => {
      await page.mouse.move(cx, cy, { steps: 4 });
      await sleep(120); // a person's hover before pressing
      await page.mouse.down();
      await sleep(80);
      const released = Date.now();
      await page.mouse.up();
      if (SHOT) {
        void sleep(SHOT_AT_MS).then(() => page.screenshot({ path: SHOT }).then(() => console.log(`  wrote ${SHOT}`)));
      }
      return released;
    });
    // Warm: back to the Timeline inside the app (no reload), then the same row.
    await page.locator("nav a[href='/timeline'], a[href='/timeline']").first().click();
    await page.waitForURL(`${BASE}/timeline`, { timeout: 30_000 });
    const row2 = page.locator(`[data-testid='session-row'][data-session-id='${SESSION_ID}']`).first();
    await row2.waitFor({ state: "visible", timeout: 30_000 });
    const cdpProfile = PROFILE ? await context.newCDPSession(page) : null;
    if (cdpProfile) {
      await cdpProfile.send("Profiler.enable");
      await cdpProfile.send("Profiler.setSamplingInterval", { interval: 200 });
      await cdpProfile.send("Profiler.start");
    }
    const warm = await timeOpen(page, async () => {
      const released = Date.now();
      await row2.click();
      return released;
    });
    if (cdpProfile) {
      const { profile } = await cdpProfile.send("Profiler.stop");
      writeFileSync(PROFILE, JSON.stringify(profile));
      console.log(`  wrote CPU profile ${PROFILE}`);
    }
    await context.close();
    return { cold, warm };
  } finally {
    await browser.close();
  }
}

function summarize(label: string, rows: Marks[]): Record<string, number> {
  const med = (xs: number[]) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)];
  const out = {
    url: med(rows.map((r) => r.url)),
    header: med(rows.map((r) => r.header)),
    firstRow: med(rows.map((r) => r.firstRow)),
    full: med(rows.map((r) => r.full)),
  };
  console.log(`${label.padEnd(5)} median ms  url=${out.url}  header=${out.header}  firstRow=${out.firstRow}  full=${out.full}`);
  return out;
}

async function main(): Promise<void> {
  const started = Date.now();
  const web = path.join(REPO_ROOT, "web");
  if (BUILD) execSync("bunx vite build --logLevel error", { cwd: web, stdio: "inherit" });
  else if (!existsSync(path.join(web, "dist", "index.html"))) throw new Error("--no-build but web/dist has no build");
  if (await isServing(BASE)) throw new Error(`${BASE} is already serving; pick another --port`);
  const child = spawn("bunx", ["vite", "preview", "--port", String(PORT), "--strictPort"], { cwd: web, stdio: "ignore", detached: true });
  const stop = () => {
    try {
      if (child.pid) process.kill(-child.pid, "SIGTERM");
    } catch {
      /* gone */
    }
  };
  process.on("SIGINT", () => {
    stop();
    process.exit(130);
  });
  try {
    for (let i = 0; i < 60 && !(await isServing(BASE)); i++) await sleep(250);
    if (!(await isServing(BASE))) {
      throw new Error(`vite preview never answered at ${BASE}${BUILD ? "" : "; is web/dist current? (drop --no-build)"}`);
    }
    const runs: { cold: Marks; warm: Marks }[] = [];
    for (let i = 0; i < RUNS; i++) {
      const r = await oneRun();
      console.log(`run ${i + 1}: cold ${JSON.stringify(r.cold)}  warm ${JSON.stringify(r.warm)}`);
      runs.push(r);
    }
    const result = {
      params: { RUNS, WORKSPACE_MS, RTT_MS, DOWN_MBPS, SETTLE_MS },
      cold: summarize("cold", runs.map((r) => r.cold)),
      warm: summarize("warm", runs.map((r) => r.warm)),
      runs,
      unmocked: [...unmocked],
    };
    if (unmocked.size) console.log(`unmocked (404): ${[...unmocked].join(", ")}`);
    if (OUT) {
      mkdirSync(path.dirname(OUT), { recursive: true });
      writeFileSync(OUT, JSON.stringify(result, null, 2));
      console.log(`wrote ${OUT}`);
    }
  } finally {
    stop();
    console.log(`elapsed ${((Date.now() - started) / 1000).toFixed(1)} s`);
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
