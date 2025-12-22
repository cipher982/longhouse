#!/usr/bin/env bun
/**
 * Landing Page Performance Profiler
 *
 * Uses Playwright + CDP to collect performance traces for different effect variants.
 * Outputs Paint/Rasterize/Composite metrics to identify GPU costs.
 *
 * Usage:
 *   bun run scripts/profile-landing.ts [options]
 *
 * Options:
 *   --url=<base>       Base URL (default: http://localhost:30080)
 *   --duration=<sec>   Idle recording duration per variant (default: 10)
 *   --headless         Run headless (default: headful for real GPU metrics)
 *   --output=<dir>     Output directory (default: ./perf-results)
 *   --variants=<list>  Comma-separated variants to test (default: none,particles,hero,all)
 */

import { chromium, type CDPSession, type Browser, type Page } from "playwright";
import * as fs from "fs";
import * as path from "path";

interface TraceEvent {
  name: string;
  cat: string;
  ph: string; // Phase: B=begin, E=end, X=complete, etc.
  ts: number; // Timestamp in microseconds
  dur?: number; // Duration in microseconds (for X events)
  pid: number;
  tid: number;
  args?: Record<string, unknown>;
}

interface TraceData {
  traceEvents: TraceEvent[];
}

interface RenderingMetrics {
  paintCount: number;
  paintTotalMs: number;
  rasterizeCount: number;
  rasterizeTotalMs: number;
  compositeCount: number;
  compositeTotalMs: number;
  updateLayerTreeCount: number;
  updateLayerTreeTotalMs: number;
  frameCount: number;
  avgFrameTimeMs: number;
  p95FrameTimeMs: number;
  maxFrameTimeMs: number;
  jankFrames: number; // frames > 16.67ms
  severeJankFrames: number; // frames > 33.33ms
}

interface VariantResult {
  variant: string;
  url: string;
  metrics: RenderingMetrics;
  traceFile: string;
}

const VARIANTS = ["none", "particles", "hero", "all"] as const;
type Variant = (typeof VARIANTS)[number];

function parseArgs(): {
  baseUrl: string;
  duration: number;
  headless: boolean;
  outputDir: string;
  variants: Variant[];
} {
  const args = process.argv.slice(2);
  let baseUrl = "http://localhost:30080";
  let duration = 10;
  let headless = false;
  let outputDir = "./perf-results";
  let variants: Variant[] = [...VARIANTS];

  for (const arg of args) {
    if (arg.startsWith("--url=")) {
      baseUrl = arg.slice(6);
    } else if (arg.startsWith("--duration=")) {
      duration = parseInt(arg.slice(11), 10);
    } else if (arg === "--headless") {
      headless = true;
    } else if (arg.startsWith("--output=")) {
      outputDir = arg.slice(9);
    } else if (arg.startsWith("--variants=")) {
      variants = arg
        .slice(11)
        .split(",")
        .map((v) => v.trim() as Variant)
        .filter((v) => VARIANTS.includes(v));
    }
  }

  return { baseUrl, duration, headless, outputDir, variants };
}

async function collectTrace(
  page: Page,
  cdp: CDPSession,
  durationMs: number
): Promise<TraceData> {
  const allEvents: TraceEvent[] = [];

  // Set up event listener BEFORE starting trace
  cdp.on("Tracing.dataCollected", (event) => {
    // value is an array of trace event objects (already parsed)
    const events = event.value as TraceEvent[];
    allEvents.push(...events);
  });

  // Start tracing with categories that capture rendering pipeline
  await cdp.send("Tracing.start", {
    categories: [
      "devtools.timeline",
      "disabled-by-default-devtools.timeline",
      "disabled-by-default-devtools.timeline.frame",
      "blink.console",
      "cc",
      "gpu",
    ].join(","),
    options: "sampling-frequency=10000", // 10kHz sampling
    transferMode: "ReportEvents", // Get events via dataCollected
  });

  // Wait for the specified idle duration
  await page.waitForTimeout(durationMs);

  // Stop tracing and wait for completion
  const traceComplete = new Promise<void>((resolve) => {
    cdp.once("Tracing.tracingComplete", () => {
      resolve();
    });
  });

  await cdp.send("Tracing.end");
  await traceComplete;

  return { traceEvents: allEvents };
}

// Chrome trace event name mappings (actual names from devtools.timeline)
const PAINT_EVENTS = new Set([
  "PrePaint",
  "Paint",
  "Layerize",
]);

const LAYOUT_EVENTS = new Set([
  "UpdateLayoutTree",
  "Layout",
  "UpdateLayerTree",
  "LayerTreeImpl::UpdateDrawProperties",
]);

const RASTERIZE_EVENTS = new Set([
  "TileManager::PrepareTiles",
  "TileManager::ScheduleTasks",
  "DidFinishRunningAllTilesTask::RunOnWorkerThread",
  "TaskSetFinishedTaskImpl::RunOnWorkerThread",
  "RasterTask",
  "Rasterize",
]);

const COMPOSITE_EVENTS = new Set([
  "MainFrame.Draw",
  "ProxyImpl::ScheduledActionDraw",
  "LayerTreeHostImpl::PrepareToDraw",
  "LayerTreeHostImpl::CalculateRenderPasses",
  "DrawLayers.FrameViewerTracing",
  "Commit",
  "CommitPresentedFrameToCA",
  "CompositeLayers",
]);

const ANIMATION_EVENTS = new Set([
  "AnimationHost::TickAnimations",
  "AnimationHost::UpdateAnimationState",
]);

function parseTraceMetrics(trace: TraceData): RenderingMetrics {
  const events = trace.traceEvents;

  let paintCount = 0;
  let paintTotalUs = 0;
  let rasterizeCount = 0;
  let rasterizeTotalUs = 0;
  let compositeCount = 0;
  let compositeTotalUs = 0;
  let updateLayerTreeCount = 0;
  let updateLayerTreeTotalUs = 0;

  const frameTimes: number[] = [];
  const frameStarts: number[] = [];

  // Track begin/end pairs
  const beginEvents = new Map<string, TraceEvent>();

  for (const event of events) {
    if (!event || !event.name) continue;

    // Handle complete events (X phase)
    if (event.ph === "X" && event.dur) {
      const durUs = event.dur;

      if (PAINT_EVENTS.has(event.name)) {
        paintCount++;
        paintTotalUs += durUs;
      } else if (RASTERIZE_EVENTS.has(event.name)) {
        rasterizeCount++;
        rasterizeTotalUs += durUs;
      } else if (COMPOSITE_EVENTS.has(event.name)) {
        compositeCount++;
        compositeTotalUs += durUs;
      } else if (LAYOUT_EVENTS.has(event.name)) {
        updateLayerTreeCount++;
        updateLayerTreeTotalUs += durUs;
      }
    }

    // Track frame timing via BeginFrame events
    if (event.name === "Scheduler::BeginFrame" || event.name === "BeginFrame") {
      frameStarts.push(event.ts);
    }

    // Handle begin/end pairs (B/E phases)
    if (event.ph === "B") {
      const key = `${event.pid}:${event.tid}:${event.name}`;
      beginEvents.set(key, event);
    } else if (event.ph === "E") {
      const key = `${event.pid}:${event.tid}:${event.name}`;
      const begin = beginEvents.get(key);
      if (begin) {
        const durUs = event.ts - begin.ts;
        beginEvents.delete(key);

        if (PAINT_EVENTS.has(event.name)) {
          paintCount++;
          paintTotalUs += durUs;
        } else if (RASTERIZE_EVENTS.has(event.name)) {
          rasterizeCount++;
          rasterizeTotalUs += durUs;
        } else if (COMPOSITE_EVENTS.has(event.name)) {
          compositeCount++;
          compositeTotalUs += durUs;
        } else if (LAYOUT_EVENTS.has(event.name)) {
          updateLayerTreeCount++;
          updateLayerTreeTotalUs += durUs;
        }
      }
    }
  }

  // Calculate frame times from BeginFrame timestamps
  frameStarts.sort((a, b) => a - b);
  for (let i = 1; i < frameStarts.length; i++) {
    const frameTime = (frameStarts[i] - frameStarts[i - 1]) / 1000; // us to ms
    if (frameTime > 0 && frameTime < 1000) {
      frameTimes.push(frameTime);
    }
  }

  // Calculate frame statistics
  const sortedFrames = [...frameTimes].sort((a, b) => a - b);
  const avgFrameTimeMs =
    frameTimes.length > 0
      ? frameTimes.reduce((a, b) => a + b, 0) / frameTimes.length
      : 0;
  const p95FrameTimeMs =
    sortedFrames.length > 0
      ? sortedFrames[Math.floor(sortedFrames.length * 0.95)] ?? avgFrameTimeMs
      : 0;
  const maxFrameTimeMs =
    sortedFrames.length > 0 ? sortedFrames[sortedFrames.length - 1] ?? 0 : 0;
  const jankFrames = frameTimes.filter((t) => t > 16.67).length;
  const severeJankFrames = frameTimes.filter((t) => t > 33.33).length;

  return {
    paintCount,
    paintTotalMs: paintTotalUs / 1000,
    rasterizeCount,
    rasterizeTotalMs: rasterizeTotalUs / 1000,
    compositeCount,
    compositeTotalMs: compositeTotalUs / 1000,
    updateLayerTreeCount,
    updateLayerTreeTotalMs: updateLayerTreeTotalUs / 1000,
    frameCount: frameTimes.length,
    avgFrameTimeMs,
    p95FrameTimeMs,
    maxFrameTimeMs,
    jankFrames,
    severeJankFrames,
  };
}

function formatMetrics(metrics: RenderingMetrics): string {
  return `
  Paint:           ${metrics.paintCount.toString().padStart(5)} calls, ${metrics.paintTotalMs.toFixed(1).padStart(8)}ms total
  Rasterize:       ${metrics.rasterizeCount.toString().padStart(5)} calls, ${metrics.rasterizeTotalMs.toFixed(1).padStart(8)}ms total
  Composite:       ${metrics.compositeCount.toString().padStart(5)} calls, ${metrics.compositeTotalMs.toFixed(1).padStart(8)}ms total
  UpdateLayerTree: ${metrics.updateLayerTreeCount.toString().padStart(5)} calls, ${metrics.updateLayerTreeTotalMs.toFixed(1).padStart(8)}ms total

  Frames:          ${metrics.frameCount} total
  Frame time:      avg=${metrics.avgFrameTimeMs.toFixed(1)}ms, p95=${metrics.p95FrameTimeMs.toFixed(1)}ms, max=${metrics.maxFrameTimeMs.toFixed(1)}ms
  Jank (>16.7ms):  ${metrics.jankFrames} frames (${((metrics.jankFrames / Math.max(1, metrics.frameCount)) * 100).toFixed(1)}%)
  Severe (>33ms):  ${metrics.severeJankFrames} frames (${((metrics.severeJankFrames / Math.max(1, metrics.frameCount)) * 100).toFixed(1)}%)`;
}

async function profileVariant(
  browser: Browser,
  baseUrl: string,
  variant: Variant,
  durationMs: number,
  outputDir: string
): Promise<VariantResult> {
  const url = `${baseUrl}/?fx=${variant}`;
  console.log(`\n  Profiling variant: ${variant}`);
  console.log(`  URL: ${url}`);
  console.log(`  Duration: ${durationMs / 1000}s`);

  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
  });
  const page = await context.newPage();

  // Create CDP session
  const cdp = await context.newCDPSession(page);

  // Navigate and wait for load
  await page.goto(url, { waitUntil: "networkidle" });

  // Give animations time to start
  await page.waitForTimeout(1000);

  console.log(`  Recording ${durationMs / 1000}s of idle activity...`);
  const trace = await collectTrace(page, cdp, durationMs);

  // Save raw trace
  const traceFile = path.join(outputDir, `trace-${variant}.json`);
  fs.writeFileSync(traceFile, JSON.stringify(trace, null, 2));

  const metrics = parseTraceMetrics(trace);

  await context.close();

  return {
    variant,
    url,
    metrics,
    traceFile,
  };
}

async function main() {
  const { baseUrl, duration, headless, outputDir, variants } = parseArgs();
  const durationMs = duration * 1000;

  console.log("\n========================================");
  console.log("Landing Page Performance Profiler");
  console.log("========================================");
  console.log(`Base URL:   ${baseUrl}`);
  console.log(`Duration:   ${duration}s per variant`);
  console.log(`Headless:   ${headless}`);
  console.log(`Variants:   ${variants.join(", ")}`);
  console.log(`Output:     ${outputDir}`);

  // Create output directory
  fs.mkdirSync(outputDir, { recursive: true });

  // Launch browser
  console.log("\nLaunching browser...");
  const browser = await chromium.launch({
    headless,
    args: [
      "--enable-gpu",
      "--enable-gpu-rasterization",
      "--enable-zero-copy",
      // Disable throttling for accurate measurements
      "--disable-backgrounding-occluded-windows",
      "--disable-renderer-backgrounding",
    ],
  });

  const results: VariantResult[] = [];

  try {
    for (const variant of variants) {
      const result = await profileVariant(
        browser,
        baseUrl,
        variant,
        durationMs,
        outputDir
      );
      results.push(result);
      console.log(formatMetrics(result.metrics));
    }
  } finally {
    await browser.close();
  }

  // Generate summary
  console.log("\n========================================");
  console.log("SUMMARY - Relative to baseline (fx=none)");
  console.log("========================================");

  const baseline = results.find((r) => r.variant === "none");
  if (baseline) {
    for (const result of results) {
      if (result.variant === "none") continue;

      const paintDiff = result.metrics.paintTotalMs - baseline.metrics.paintTotalMs;
      const rasterDiff = result.metrics.rasterizeTotalMs - baseline.metrics.rasterizeTotalMs;
      const compositeDiff = result.metrics.compositeTotalMs - baseline.metrics.compositeTotalMs;
      const jankDiff = result.metrics.jankFrames - baseline.metrics.jankFrames;

      console.log(`\n${result.variant} vs none:`);
      console.log(`  Paint:     ${paintDiff >= 0 ? "+" : ""}${paintDiff.toFixed(1)}ms`);
      console.log(`  Rasterize: ${rasterDiff >= 0 ? "+" : ""}${rasterDiff.toFixed(1)}ms`);
      console.log(`  Composite: ${compositeDiff >= 0 ? "+" : ""}${compositeDiff.toFixed(1)}ms`);
      console.log(`  Jank:      ${jankDiff >= 0 ? "+" : ""}${jankDiff} frames`);
    }
  }

  // Save summary JSON
  const summaryFile = path.join(outputDir, "summary.json");
  const summary = {
    timestamp: new Date().toISOString(),
    config: { baseUrl, duration, headless, variants },
    results: results.map((r) => ({
      variant: r.variant,
      url: r.url,
      metrics: r.metrics,
      traceFile: r.traceFile,
    })),
  };
  fs.writeFileSync(summaryFile, JSON.stringify(summary, null, 2));

  // Save markdown report
  const reportFile = path.join(outputDir, "report.md");
  let report = `# Landing Page Performance Report

Generated: ${new Date().toISOString()}

## Configuration
- Base URL: ${baseUrl}
- Duration: ${duration}s per variant
- Headless: ${headless}
- Variants: ${variants.join(", ")}

## Results

| Variant | Paint (ms) | Rasterize (ms) | Composite (ms) | Jank Frames | Severe Jank |
|---------|------------|----------------|----------------|-------------|-------------|
`;

  for (const result of results) {
    const m = result.metrics;
    report += `| ${result.variant} | ${m.paintTotalMs.toFixed(1)} | ${m.rasterizeTotalMs.toFixed(1)} | ${m.compositeTotalMs.toFixed(1)} | ${m.jankFrames} | ${m.severeJankFrames} |\n`;
  }

  if (baseline) {
    report += `\n## Delta from Baseline (fx=none)\n\n`;
    report += `| Variant | Paint | Rasterize | Composite | Jank |\n`;
    report += `|---------|-------|-----------|-----------|------|\n`;

    for (const result of results) {
      if (result.variant === "none") continue;
      const paintDiff = result.metrics.paintTotalMs - baseline.metrics.paintTotalMs;
      const rasterDiff = result.metrics.rasterizeTotalMs - baseline.metrics.rasterizeTotalMs;
      const compositeDiff = result.metrics.compositeTotalMs - baseline.metrics.compositeTotalMs;
      const jankDiff = result.metrics.jankFrames - baseline.metrics.jankFrames;

      report += `| ${result.variant} | ${paintDiff >= 0 ? "+" : ""}${paintDiff.toFixed(1)}ms | ${rasterDiff >= 0 ? "+" : ""}${rasterDiff.toFixed(1)}ms | ${compositeDiff >= 0 ? "+" : ""}${compositeDiff.toFixed(1)}ms | ${jankDiff >= 0 ? "+" : ""}${jankDiff} |\n`;
    }
  }

  report += `\n## Raw Trace Files

Open these in Chrome DevTools (Performance tab → Load profile) or [Perfetto](https://ui.perfetto.dev):

`;
  for (const result of results) {
    report += `- \`${result.traceFile}\`\n`;
  }

  report += `\n## How to Interpret

- **Paint**: Time spent painting pixels (should be near-zero when idle if using compositor-only animations)
- **Rasterize**: Time converting vector graphics to pixels (expensive for complex SVGs)
- **Composite**: Time blending layers together (cheap, GPU-accelerated)
- **Jank Frames**: Frames taking >16.67ms (60fps threshold)
- **Severe Jank**: Frames taking >33.33ms (30fps threshold)

Ideally, an "idle" page should have:
- Near-zero Paint/Rasterize during idle (no repainting)
- Only Composite activity (cheap layer blending)
- Zero jank frames
`;

  fs.writeFileSync(reportFile, report);

  console.log(`\n\nResults saved to:`);
  console.log(`  Summary: ${summaryFile}`);
  console.log(`  Report:  ${reportFile}`);
  console.log(`  Traces:  ${outputDir}/trace-*.json`);
  console.log(`\nOpen traces in Chrome DevTools or https://ui.perfetto.dev`);
}

main().catch((err) => {
  console.error("Error:", err);
  process.exit(1);
});
