#!/usr/bin/env bun
/**
 * GPU Utilization Profiler for Dashboard Effects (macOS)
 *
 * Measures actual GPU utilization % (via macOS ioreg) while the dashboard idles
 * with UI effects ON vs OFF.
 *
 * Usage:
 *   bun run scripts/gpu-profiler-dashboard.ts [options]
 *
 * Options:
 *   --url=<base>       Base URL (default: http://localhost:30080)
 *   --duration=<sec>   Measurement duration per variant (default: 10)
 *   --interval=<ms>    Sampling interval (default: 500)
 *   --output=<dir>     Output directory (default: ./perf-results)
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

interface Stats {
  min: number;
  max: number;
  avg: number;
  median: number;
  p95: number;
}

type Variant = "off" | "on";

interface VariantResult {
  variant: Variant;
  url: string;
  samples: GpuSample[];
  stats: {
    deviceUtilization: Stats;
    rendererUtilization: Stats;
    tilerUtilization: Stats;
    inUseMemoryMB: Stats;
  };
}

function parseArgs() {
  const args = process.argv.slice(2);
  let baseUrl = "http://localhost:30080";
  let duration = 10;
  let interval = 500;
  let outputDir = "./perf-results";
  let warmup = 3;

  for (const arg of args) {
    if (arg.startsWith("--url=")) baseUrl = arg.slice(6);
    else if (arg.startsWith("--duration=")) duration = parseInt(arg.slice(11), 10);
    else if (arg.startsWith("--interval=")) interval = parseInt(arg.slice(11), 10);
    else if (arg.startsWith("--output=")) outputDir = arg.slice(9);
    else if (arg.startsWith("--warmup=")) warmup = parseInt(arg.slice(9), 10);
  }

  return { baseUrl, duration, interval, outputDir, warmup };
}

function sampleGpu(): GpuSample | null {
  try {
    const output = execSync('ioreg -r -d 1 -c IOAccelerator 2>/dev/null | grep "PerformanceStatistics"', {
      encoding: "utf-8",
      timeout: 2000,
    });

    const match = output.match(/PerformanceStatistics.*?\\{([^}]+)\\}/);
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
  if (values.length === 0) return { min: 0, max: 0, avg: 0, median: 0, p95: 0 };
  const sorted = [...values].sort((a, b) => a - b);
  const sum = sorted.reduce((acc, v) => acc + v, 0);
  return {
    min: sorted[0],
    max: sorted[sorted.length - 1],
    avg: sum / sorted.length,
    median: sorted[Math.floor(sorted.length / 2)],
    p95: sorted[Math.floor(sorted.length * 0.95)] ?? sorted[sorted.length - 1],
  };
}

function formatStats(name: string, stats: Stats, unit: string = "%"): string {
  return `  ${name.padEnd(20)} avg=${stats.avg.toFixed(1).padStart(5)}${unit}  median=${stats.median.toFixed(1).padStart(5)}${unit}  p95=${stats.p95.toFixed(1).padStart(5)}${unit}  range=[${stats.min.toFixed(0)}-${stats.max.toFixed(0)}${unit}]`;
}

async function measureVariant(
  browser: Browser,
  baseUrl: string,
  variant: Variant,
  durationSec: number,
  intervalMs: number,
  warmupSec: number
): Promise<VariantResult> {
  const url = `${baseUrl}/dashboard?uieffects=${variant}`;
  console.log(`\n  Measuring: dashboard ui-effects=${variant}`);
  console.log(`  URL: ${url}`);

  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
  });
  const page = await context.newPage();

  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await context.clearCookies();
  await page.evaluate(() => {
    try {
      localStorage.clear();
      sessionStorage.clear();
    } catch {}
  });

  await page.goto(url, { waitUntil: "networkidle" });

  const isDashboard = await page.evaluate(() => !!document.querySelector("#dashboard-container"));
  if (!isDashboard) {
    console.log(`  ⚠️  WARNING: Not on dashboard. Final URL: ${page.url()}`);
  }

  console.log(`  Warmup: ${warmupSec}s...`);
  await page.waitForTimeout(warmupSec * 1000);

  console.log(`  Sampling: ${durationSec}s @ ${intervalMs}ms intervals...`);
  const samples: GpuSample[] = [];
  const endTime = Date.now() + durationSec * 1000;
  while (Date.now() < endTime) {
    const sample = sampleGpu();
    if (sample) samples.push(sample);
    await page.waitForTimeout(intervalMs);
  }

  await context.close();

  const stats = {
    deviceUtilization: calculateStats(samples.map((s) => s.deviceUtilization)),
    rendererUtilization: calculateStats(samples.map((s) => s.rendererUtilization)),
    tilerUtilization: calculateStats(samples.map((s) => s.tilerUtilization)),
    inUseMemoryMB: calculateStats(samples.map((s) => s.inUseMemoryMB)),
  };

  return { variant, url, samples, stats };
}

async function main() {
  const { baseUrl, duration, interval, outputDir, warmup } = parseArgs();

  console.log("\n========================================");
  console.log("GPU Utilization Profiler (Dashboard)");
  console.log("========================================");
  console.log(`Base URL:   ${baseUrl}`);
  console.log(`Duration:   ${duration}s per variant`);
  console.log(`Interval:   ${interval}ms`);
  console.log(`Warmup:     ${warmup}s`);
  console.log(`Output:     ${outputDir}`);

  const testSample = sampleGpu();
  if (!testSample) {
    console.error("\nERROR: Cannot read GPU stats from ioreg.");
    process.exit(1);
  }
  console.log(`\nGPU detected: Current utilization ${testSample.deviceUtilization}%`);

  fs.mkdirSync(outputDir, { recursive: true });

  console.log("\n--- Baseline (no browser) ---");
  const baselineSamples: number[] = [];
  const baselineEnd = Date.now() + Math.min(5, duration) * 1000;
  while (Date.now() < baselineEnd) {
    const s = sampleGpu();
    if (s) baselineSamples.push(s.deviceUtilization);
    await new Promise((r) => setTimeout(r, interval));
  }
  const baselineStats = calculateStats(baselineSamples);
  console.log(formatStats("Device Util %", baselineStats));

  const browser = await chromium.launch({
    headless: false,
    args: ["--enable-gpu", "--enable-gpu-rasterization"],
  });

  console.log("\n--- Reference (about:blank) ---");
  {
    const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
    const page = await context.newPage();
    await page.goto("about:blank");
    await page.waitForTimeout(1500);

    const samples: GpuSample[] = [];
    const endTime = Date.now() + Math.min(6, duration) * 1000;
    while (Date.now() < endTime) {
      const sample = sampleGpu();
      if (sample) samples.push(sample);
      await page.waitForTimeout(interval);
    }
    const stats = calculateStats(samples.map((s) => s.deviceUtilization));
    console.log(formatStats("Device Util %", stats));
    await context.close();
  }

  console.log("\n--- Dashboard Variants ---");
  const variants: Variant[] = ["off", "on"];
  const results: VariantResult[] = [];
  for (const v of variants) {
    results.push(await measureVariant(browser, baseUrl, v, duration, interval, warmup));
  }

  await browser.close();

  const summary = {
    meta: {
      date: new Date().toISOString(),
      baseUrl,
      durationSec: duration,
      intervalMs: interval,
      warmupSec: warmup,
    },
    baselineNoBrowser: baselineStats,
    variants: results.map((r) => ({
      variant: r.variant,
      url: r.url,
      stats: r.stats,
    })),
  };

  const samplesOut = {
    meta: summary.meta,
    variants: results.map((r) => ({
      variant: r.variant,
      url: r.url,
      samples: r.samples,
    })),
  };

  const reportLines: string[] = [];
  reportLines.push("# GPU Dashboard Report");
  reportLines.push("");
  reportLines.push(`- Date: ${summary.meta.date}`);
  reportLines.push(`- Base URL: ${baseUrl}`);
  reportLines.push(`- Duration: ${duration}s per variant`);
  reportLines.push(`- Interval: ${interval}ms`);
  reportLines.push(`- Warmup: ${warmup}s`);
  reportLines.push("");
  reportLines.push("## Baseline (no browser)");
  reportLines.push("");
  reportLines.push("```");
  reportLines.push(formatStats("Device Util %", baselineStats));
  reportLines.push("```");
  reportLines.push("");
  reportLines.push("## Dashboard");
  reportLines.push("");
  for (const r of results) {
    reportLines.push(`### ui-effects=${r.variant}`);
    reportLines.push("");
    reportLines.push("```");
    reportLines.push(formatStats("Device Util %", r.stats.deviceUtilization));
    reportLines.push(formatStats("Renderer Util %", r.stats.rendererUtilization));
    reportLines.push(formatStats("Tiler Util %", r.stats.tilerUtilization));
    reportLines.push(formatStats("GPU Mem (MB)", r.stats.inUseMemoryMB, "MB"));
    reportLines.push("```");
    reportLines.push("");
  }

  fs.writeFileSync(path.join(outputDir, "gpu-dashboard-summary.json"), JSON.stringify(summary, null, 2));
  fs.writeFileSync(path.join(outputDir, "gpu-dashboard-samples.json"), JSON.stringify(samplesOut, null, 2));
  fs.writeFileSync(path.join(outputDir, "gpu-dashboard-report.md"), reportLines.join("\n"));

  console.log("\n========================================");
  console.log("DONE");
  console.log("========================================");
  console.log(`\nWrote: ${path.join(outputDir, "gpu-dashboard-report.md")}`);
  console.log(`Wrote: ${path.join(outputDir, "gpu-dashboard-summary.json")}`);
  console.log(`Wrote: ${path.join(outputDir, "gpu-dashboard-samples.json")}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
