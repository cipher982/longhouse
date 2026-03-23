#!/usr/bin/env bun
/**
 * GPU Utilization Profiler for Landing Page Effects
 *
 * Measures actual GPU utilization % (via macOS ioreg) while each effect variant
 * runs idle. This tells you the real-world GPU cost of CSS animations.
 *
 * Usage:
 *   bun run scripts/gpu-profiler.ts [options]
 *
 * Options:
 *   --url=<base>       Base URL (default: http://localhost:30080)
 *   --duration=<sec>   Measurement duration per variant (default: 10)
 *   --interval=<ms>    Sampling interval (default: 500)
 *   --output=<dir>     Output directory (default: ./perf-results)
 *   --variants=<list>  Comma-separated variants (default: none,particles,hero,all)
 *   --warmup=<sec>     Warmup time before measuring (default: 3)
 */

import { chromium, type Browser } from "playwright";
import { execSync } from "child_process";
import * as fs from "fs";
import * as path from "path";

interface GpuSample {
  timestamp: number;
  deviceUtilization: number;
  rendererUtilization: number;
  tilerUtilization: number;
  inUseMemoryMB: number;
}

interface VariantResult {
  variant: string;
  url: string;
  samples: GpuSample[];
  stats: {
    deviceUtilization: Stats;
    rendererUtilization: Stats;
    tilerUtilization: Stats;
    inUseMemoryMB: Stats;
  };
}

interface Stats {
  min: number;
  max: number;
  avg: number;
  median: number;
  p95: number;
}

const VARIANTS = ["none", "particles", "hero", "all"] as const;
type Variant = (typeof VARIANTS)[number];

// Reference URLs for comparison baselines
const REFERENCE_URLS = {
  "blank": "about:blank",
  "google": "https://www.google.com",
} as const;

function parseArgs() {
  const args = process.argv.slice(2);
  let baseUrl = "http://localhost:30080";
  let duration = 10;
  let interval = 500;
  let outputDir = "./perf-results";
  let variants: Variant[] = [...VARIANTS];
  let warmup = 3;

  for (const arg of args) {
    if (arg.startsWith("--url=")) baseUrl = arg.slice(6);
    else if (arg.startsWith("--duration=")) duration = parseInt(arg.slice(11), 10);
    else if (arg.startsWith("--interval=")) interval = parseInt(arg.slice(11), 10);
    else if (arg.startsWith("--output=")) outputDir = arg.slice(9);
    else if (arg.startsWith("--warmup=")) warmup = parseInt(arg.slice(9), 10);
    else if (arg.startsWith("--variants=")) {
      variants = arg
        .slice(11)
        .split(",")
        .map((v) => v.trim() as Variant)
        .filter((v) => VARIANTS.includes(v));
    }
  }

  return { baseUrl, duration, interval, outputDir, variants, warmup };
}

/**
 * Sample GPU stats from macOS ioreg (no sudo required)
 */
function sampleGpu(): GpuSample | null {
  try {
    const output = execSync(
      'ioreg -r -d 1 -c IOAccelerator 2>/dev/null | grep "PerformanceStatistics"',
      { encoding: "utf-8", timeout: 2000 }
    );

    // Parse the ioreg output format: "key"=value
    const match = output.match(/PerformanceStatistics.*?\{([^}]+)\}/);
    if (!match) return null;

    const statsStr = match[1];

    const getNum = (key: string): number => {
      const m = statsStr.match(new RegExp(`"${key}"=(\\d+)`));
      return m ? parseInt(m[1], 10) : 0;
    };

    return {
      timestamp: Date.now(),
      deviceUtilization: getNum("Device Utilization %"),
      rendererUtilization: getNum("Renderer Utilization %"),
      tilerUtilization: getNum("Tiler Utilization %"),
      inUseMemoryMB: Math.round(getNum("In use system memory") / 1024 / 1024),
    };
  } catch {
    return null;
  }
}

function calculateStats(values: number[]): Stats {
  if (values.length === 0) {
    return { min: 0, max: 0, avg: 0, median: 0, p95: 0 };
  }

  const sorted = [...values].sort((a, b) => a - b);
  const sum = sorted.reduce((a, b) => a + b, 0);

  return {
    min: sorted[0],
    max: sorted[sorted.length - 1],
    avg: sum / sorted.length,
    median: sorted[Math.floor(sorted.length / 2)],
    p95: sorted[Math.floor(sorted.length * 0.95)] ?? sorted[sorted.length - 1],
  };
}

async function measureVariant(
  browser: Browser,
  baseUrl: string,
  variant: Variant,
  durationSec: number,
  intervalMs: number,
  warmupSec: number
): Promise<VariantResult> {
  const url = `${baseUrl}/?fx=${variant}&noredirect=1`;
  console.log(`\n  Measuring: ${variant}`);
  console.log(`  URL: ${url}`);

  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
  });
  const page = await context.newPage();

  // Navigate once to establish domain, then clear storage
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await context.clearCookies();
  await page.evaluate(() => {
    try {
      localStorage.clear();
      sessionStorage.clear();
    } catch {}
  });

  // Now navigate to the actual URL
  await page.goto(url, { waitUntil: "networkidle" });

  // Verify we landed on the landing page
  const finalUrl = page.url();
  const isLandingPage = await page.evaluate(() => !!document.querySelector(".landing-page"));
  if (!isLandingPage) {
    console.log(`  ⚠️  WARNING: Not on landing page! URL: ${finalUrl}`);
  }

  // Warmup period - let animations stabilize
  console.log(`  Warmup: ${warmupSec}s...`);
  await page.waitForTimeout(warmupSec * 1000);

  // Sample GPU utilization
  console.log(`  Sampling: ${durationSec}s @ ${intervalMs}ms intervals...`);
  const samples: GpuSample[] = [];
  const endTime = Date.now() + durationSec * 1000;

  while (Date.now() < endTime) {
    const sample = sampleGpu();
    if (sample) {
      samples.push(sample);
    }
    await page.waitForTimeout(intervalMs);
  }

  await context.close();

  // Calculate stats
  const stats = {
    deviceUtilization: calculateStats(samples.map((s) => s.deviceUtilization)),
    rendererUtilization: calculateStats(samples.map((s) => s.rendererUtilization)),
    tilerUtilization: calculateStats(samples.map((s) => s.tilerUtilization)),
    inUseMemoryMB: calculateStats(samples.map((s) => s.inUseMemoryMB)),
  };

  return { variant, url, samples, stats };
}

function formatStats(name: string, stats: Stats, unit: string = "%"): string {
  return `  ${name.padEnd(20)} avg=${stats.avg.toFixed(1).padStart(5)}${unit}  median=${stats.median.toFixed(1).padStart(5)}${unit}  p95=${stats.p95.toFixed(1).padStart(5)}${unit}  range=[${stats.min.toFixed(0)}-${stats.max.toFixed(0)}${unit}]`;
}

async function main() {
  const { baseUrl, duration, interval, outputDir, variants, warmup } = parseArgs();

  console.log("\n========================================");
  console.log("GPU Utilization Profiler");
  console.log("========================================");
  console.log(`Base URL:   ${baseUrl}`);
  console.log(`Duration:   ${duration}s per variant`);
  console.log(`Interval:   ${interval}ms`);
  console.log(`Warmup:     ${warmup}s`);
  console.log(`Variants:   ${variants.join(", ")}`);
  console.log(`Output:     ${outputDir}`);

  // Test ioreg access
  const testSample = sampleGpu();
  if (!testSample) {
    console.error("\nERROR: Cannot read GPU stats from ioreg.");
    console.error("This tool requires macOS with Apple Silicon or AMD GPU.");
    process.exit(1);
  }
  console.log(`\nGPU detected: Current utilization ${testSample.deviceUtilization}%`);

  // Create output directory
  fs.mkdirSync(outputDir, { recursive: true });

  // Measure baseline (no browser)
  console.log("\n--- Baseline (no browser) ---");
  const baselineSamples: GpuSample[] = [];
  console.log(`  Sampling system GPU for ${Math.min(5, duration)}s...`);
  const baselineEnd = Date.now() + Math.min(5, duration) * 1000;
  while (Date.now() < baselineEnd) {
    const sample = sampleGpu();
    if (sample) baselineSamples.push(sample);
    await new Promise((r) => setTimeout(r, interval));
  }
  const baselineStats = calculateStats(baselineSamples.map((s) => s.deviceUtilization));
  console.log(formatStats("Device Utilization", baselineStats));

  // Launch browser
  console.log("\n--- Reference Baselines (browser open) ---");
  console.log("Launching browser...");
  const browser = await chromium.launch({
    headless: false, // Must be headful for real GPU usage
    args: ["--enable-gpu", "--enable-gpu-rasterization"],
  });

  // Reference baselines
  const referenceResults: { name: string; url: string; stats: Stats }[] = [];

  for (const [name, url] of Object.entries(REFERENCE_URLS)) {
    console.log(`\n  Measuring reference: ${name} (${url})`);
    const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
    const page = await context.newPage();
    await page.goto(url, { waitUntil: "networkidle", timeout: 10000 }).catch(() => {});
    console.log(`  Warmup: ${warmup}s...`);
    await page.waitForTimeout(warmup * 1000);

    console.log(`  Sampling: ${Math.min(5, duration)}s...`);
    const samples: GpuSample[] = [];
    const endTime = Date.now() + Math.min(5, duration) * 1000;
    while (Date.now() < endTime) {
      const sample = sampleGpu();
      if (sample) samples.push(sample);
      await page.waitForTimeout(interval);
    }
    await context.close();

    const stats = calculateStats(samples.map((s) => s.deviceUtilization));
    referenceResults.push({ name, url, stats });
    console.log(formatStats("Device Utilization", stats));
  }

  // App variant tests
  console.log("\n--- Landing Page Variants ---");
  const results: VariantResult[] = [];

  try {
    for (const variant of variants) {
      const result = await measureVariant(
        browser,
        baseUrl,
        variant,
        duration,
        interval,
        warmup
      );
      results.push(result);

      console.log(`\n  Results for ${variant}:`);
      console.log(formatStats("Device Utilization", result.stats.deviceUtilization));
      console.log(formatStats("Renderer Utilization", result.stats.rendererUtilization));
      console.log(formatStats("GPU Memory", result.stats.inUseMemoryMB, "MB"));
    }
  } finally {
    await browser.close();
  }

  // Generate summary
  console.log("\n========================================");
  console.log("SUMMARY - GPU Device Utilization %");
  console.log("========================================");

  console.log(`\n--- Baselines ---`);
  console.log(`No browser:    avg=${baselineStats.avg.toFixed(1)}%`);
  for (const ref of referenceResults) {
    console.log(`${ref.name.padEnd(14)} avg=${ref.stats.avg.toFixed(1)}%`);
  }

  // Find about:blank baseline for comparison
  const blankRef = referenceResults.find((r) => r.name === "blank");
  const browserBaseline = blankRef?.stats.avg ?? baselineStats.avg;

  console.log(`\n--- Landing Page Variants ---`);
  console.log("| Variant    | Avg   | Median | P95   | vs blank    |");
  console.log("|------------|-------|--------|-------|-------------|");

  const fxNone = results.find((r) => r.variant === "none");
  for (const result of results) {
    const s = result.stats.deviceUtilization;
    const delta = s.avg - browserBaseline;
    const deltaStr = delta >= 0 ? `+${delta.toFixed(1)}%` : `${delta.toFixed(1)}%`;

    console.log(
      `| ${result.variant.padEnd(10)} | ${s.avg.toFixed(1).padStart(5)} | ${s.median.toFixed(1).padStart(6)} | ${s.p95.toFixed(1).padStart(5)} | ${deltaStr.padStart(11)} |`
    );
  }

  // Cost breakdown
  console.log("\n--- Cost Breakdown ---");
  if (fxNone) {
    const landingPageCost = fxNone.stats.deviceUtilization.avg - browserBaseline;
    console.log(`  Landing page (fx=none) vs blank page: +${landingPageCost.toFixed(1)}% GPU`);
  }

  if (fxNone) {
    console.log("\n--- Effect Cost (vs fx=none) ---");
    for (const result of results) {
      if (result.variant === "none") continue;
      const delta = result.stats.deviceUtilization.avg - fxNone.stats.deviceUtilization.avg;
      const emoji = delta > 5 ? "🔴" : delta > 2 ? "🟡" : "🟢";
      console.log(
        `  ${emoji} ${result.variant}: ${delta >= 0 ? "+" : ""}${delta.toFixed(1)}% GPU`
      );
    }
  }

  // Save results
  const summaryFile = path.join(outputDir, "gpu-summary.json");
  const summary = {
    timestamp: new Date().toISOString(),
    config: { baseUrl, duration, interval, warmup, variants },
    baselines: {
      noBrowser: { samples: baselineSamples.length, stats: { deviceUtilization: baselineStats } },
      references: referenceResults.map((r) => ({ name: r.name, url: r.url, stats: r.stats })),
    },
    results: results.map((r) => ({
      variant: r.variant,
      url: r.url,
      samples: r.samples.length,
      stats: r.stats,
    })),
  };
  fs.writeFileSync(summaryFile, JSON.stringify(summary, null, 2));

  // Save detailed samples
  const samplesFile = path.join(outputDir, "gpu-samples.json");
  fs.writeFileSync(
    samplesFile,
    JSON.stringify(
      {
        baseline: baselineSamples,
        variants: Object.fromEntries(results.map((r) => [r.variant, r.samples])),
      },
      null,
      2
    )
  );

  // Generate markdown report
  const reportFile = path.join(outputDir, "gpu-report.md");
  let report = `# GPU Utilization Report

Generated: ${new Date().toISOString()}

## Configuration
- Base URL: ${baseUrl}
- Duration: ${duration}s per variant
- Sampling interval: ${interval}ms
- Warmup: ${warmup}s

## System Baseline
GPU utilization with no browser: **${baselineStats.avg.toFixed(1)}%** avg

## Results

| Variant | Avg GPU % | Median | P95 | Delta vs none |
|---------|-----------|--------|-----|---------------|
`;

  for (const result of results) {
    const s = result.stats.deviceUtilization;
    const delta = fxNone
      ? s.avg - fxNone.stats.deviceUtilization.avg
      : 0;
    const deltaStr = result.variant === "none" ? "-" : `${delta >= 0 ? "+" : ""}${delta.toFixed(1)}%`;
    report += `| ${result.variant} | ${s.avg.toFixed(1)}% | ${s.median.toFixed(1)}% | ${s.p95.toFixed(1)}% | ${deltaStr} |\n`;
  }

  report += `
## Interpretation

- **< 2% delta**: Effect is essentially free (compositor-only)
- **2-5% delta**: Minor GPU cost, acceptable for most use cases
- **> 5% delta**: Significant GPU cost, may cause battery drain / heat

## What This Measures

This measures actual GPU utilization % from the macOS GPU driver (via \`ioreg\`).
This is the same metric shown in Activity Monitor's GPU History.

A page with no animations should have near-baseline GPU usage.
CSS animations that trigger continuous Paint/Rasterize will show elevated GPU %.
Compositor-only animations (transform, opacity) should be nearly free.
`;

  fs.writeFileSync(reportFile, report);

  console.log(`\n\nResults saved to:`);
  console.log(`  Report:  ${reportFile}`);
  console.log(`  Summary: ${summaryFile}`);
  console.log(`  Samples: ${samplesFile}`);
}

main().catch((err) => {
  console.error("Error:", err);
  process.exit(1);
});
