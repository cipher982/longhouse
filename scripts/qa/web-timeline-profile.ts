/**
 * Profile the production web timeline with real-sized data.
 *
 *   bun scripts/qa/web-timeline-profile.ts --payload=<timeline.json> [--runs=3] [--throttle=4]
 *
 * Serves web/dist (build it first: cd web && bun run build) with every API
 * call answered here: the timeline from --payload (a real
 * /api/timeline/sessions body), an authenticated shell, everything else an
 * empty 200 and logged. Records a CDP CPU profile under CPU throttling across
 * load and a few scrolls, maps samples through the build's source maps, and
 * prints load timings, long tasks, and the hottest source functions.
 * playwright and source-map resolve from the workspace's root node_modules
 * (bun install at the repo root).
 */
import { readFileSync, existsSync } from "node:fs";
import { createServer } from "node:http";
import { join, extname } from "node:path";
import { chromium } from "playwright";
import { SourceMapConsumer } from "source-map";

const args = Object.fromEntries(
  process.argv.slice(2).map((arg) => {
    const [key, value] = arg.replace(/^--/, "").split("=");
    return [key, value ?? "1"];
  }),
);
const payloadPath = args.payload;
if (!payloadPath) throw new Error("--payload=<timeline.json> is required");
const runs = Number(args.runs ?? 3);
const throttle = Number(args.throttle ?? 4);
const dist = join(import.meta.dir, "../../web/dist");
const payload = readFileSync(payloadPath, "utf8");

const TYPES: Record<string, string> = {
  ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".map": "application/json",
  ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2", ".json": "application/json",
};
const server = createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://x");
  if (url.pathname === "/config.js") {
    // What the Runtime Host serves at /config.js; the app refuses to boot without it.
    res.writeHead(200, { "content-type": "text/javascript" });
    res.end(`window.API_BASE_URL="/api";\nwindow.WS_BASE_URL="ws://${req.headers.host}";\n`);
    return;
  }
  let file = join(dist, decodeURIComponent(url.pathname));
  if (!file.startsWith(dist) || !existsSync(file) || url.pathname === "/" || !extname(file)) file = join(dist, "index.html");
  res.writeHead(200, { "content-type": TYPES[extname(file)] ?? "application/octet-stream" });
  res.end(readFileSync(file));
});
await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${(server.address() as { port: number }).port}`;

const SHELL: Record<string, unknown> = {
  "/api/auth/status": {
    authenticated: true,
    user: { id: 1, email: "perf@example.com", display_name: "Perf", avatar_url: null, is_active: true, created_at: "2026-01-01T00:00:00Z", role: "ADMIN" },
  },
  "/api/users/me": { id: 1, email: "perf@example.com", display_name: "Perf", avatar_url: null, is_active: true, role: "ADMIN" },
  "/api/auth/methods": { google: false, password: false, sso: false, sso_url: null },
  "/api/health": { status: "healthy" },
  "/api/runners/status": { total: 0, online: 0, offline: 0, runners: [] },
};

type Frame = { functionName: string; url: string; lineNumber: number; columnNumber: number };
type ProfileNode = { id: number; callFrame: Frame; children?: number[] };
type Profile = { nodes: ProfileNode[]; samples: number[]; timeDeltas: number[] };

// Outside a browser the source-map package needs its WASM handed to it.
// Bun resolves the package's browser build, which fetches it by URL.
(SourceMapConsumer as unknown as { initialize(opts: Record<string, string>): void }).initialize({
  "lib/mappings.wasm": `file://${join(import.meta.dir, "../../node_modules/source-map/lib/mappings.wasm")}`,
});
const consumers = new Map<string, SourceMapConsumer | null>();
async function original(frame: Frame): Promise<string> {
  const file = frame.url.replace(origin, "");
  if (!file.startsWith("/assets/")) return frame.functionName || "(native)";
  if (!consumers.has(file)) {
    const mapPath = join(dist, `${file}.map`);
    consumers.set(file, existsSync(mapPath) ? await new SourceMapConsumer(JSON.parse(readFileSync(mapPath, "utf8"))) : null);
  }
  const consumer = consumers.get(file);
  if (!consumer) return `${frame.functionName || "(anon)"} ${file}`;
  const pos = consumer.originalPositionFor({ line: frame.lineNumber + 1, column: frame.columnNumber });
  const source = (pos.source ?? "?").replace(/^.*?(src\/|node_modules\/)/, "$1");
  return `${pos.name ?? frame.functionName ?? "(anon)"} ${source}:${pos.line ?? "?"}`;
}

const unmocked = new Set<string>();
const browser = await chromium.launch();
const totals = new Map<string, number>();
const summaries: string[] = [];
try {
  for (let run = 0; run < runs; run++) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.route(`${origin}/api/**`, async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === "/api/timeline/sessions") return route.fulfill({ status: 200, contentType: "application/json", body: payload });
      if (pathname.endsWith("/stream")) return route.fulfill({ status: 204, body: "" });
      if (pathname in SHELL) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SHELL[pathname]) });
      unmocked.add(`${route.request().method()} ${pathname}`);
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });
    const page = await context.newPage();
    await page.addInitScript(() => {
      (window as any).__longTasks = [];
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) (window as any).__longTasks.push(entry.duration);
      }).observe({ type: "longtask", buffered: true });
    });
    const cdp = await context.newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: throttle });
    await cdp.send("Profiler.enable");
    await cdp.send("Profiler.setSamplingInterval", { interval: 200 });
    await cdp.send("Profiler.start");
    const started = Date.now();
    await page.goto(`${origin}/timeline`, { waitUntil: "load" });
    await page.waitForFunction(() => document.body.innerText.length > 2000, null, { timeout: 60_000 });
    const firstContentMs = Date.now() - started;
    for (let i = 0; i < 6; i++) {
      await page.mouse.wheel(0, 1600);
      await page.waitForTimeout(250);
    }
    await page.waitForTimeout(500);
    const { profile } = (await cdp.send("Profiler.stop")) as { profile: Profile };
    const vitals = await page.evaluate(() => {
      const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming;
      const long = (window as any).__longTasks as number[];
      return {
        domContentLoaded: Math.round(nav.domContentLoadedEventEnd),
        longTasks: long.length,
        longTaskMs: Math.round(long.reduce((a, b) => a + b, 0)),
        worstLongTaskMs: Math.round(Math.max(0, ...long)),
        domNodes: document.getElementsByTagName("*").length,
      };
    });
    summaries.push(`run ${run + 1}: first content ${firstContentMs} ms, ${JSON.stringify(vitals)}`);

    const byId = new Map(profile.nodes.map((node) => [node.id, node]));
    for (let i = 0; i < profile.samples.length; i++) {
      const node = byId.get(profile.samples[i]);
      if (!node) continue;
      const name = node.callFrame.url ? await original(node.callFrame) : node.callFrame.functionName || "(program)";
      if (name === "(idle)" || name === "(program)") continue;
      totals.set(name, (totals.get(name) ?? 0) + (profile.timeDeltas[i] ?? 0) / 1000);
    }
    await context.close();
  }
} finally {
  await browser.close();
  server.close();
}

console.log(`throttle ${throttle}x, ${runs} runs, payload ${(payload.length / 1024).toFixed(0)} KB`);
for (const line of summaries) console.log(line);
console.log("\nHottest functions, self time summed over runs (throttled ms):");
for (const [name, ms] of [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 25)) {
  console.log(`  ${ms.toFixed(0).padStart(7)}  ${name}`);
}
if (unmocked.size) console.log(`\nunmocked (answered {}): ${[...unmocked].sort().join(", ")}`);
