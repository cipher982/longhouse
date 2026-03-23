#!/usr/bin/env bun
/**
 * GPU CSS Property Diagnostic - Find which CSS features eat GPU
 *
 * Tests disabling specific CSS properties that are known GPU-heavy.
 */

import { chromium, type Page } from "playwright";
import { execSync } from "child_process";

function sampleGpu(): number | null {
  try {
    const output = execSync(
      'ioreg -r -d 1 -c IOAccelerator 2>/dev/null | grep "PerformanceStatistics"',
      { encoding: "utf-8", timeout: 2000 }
    );
    const match = output.match(/"Device Utilization %"=(\d+)/);
    return match ? parseInt(match[1], 10) : null;
  } catch {
    return null;
  }
}

async function measureGpu(page: Page, durationMs: number): Promise<number> {
  const samples: number[] = [];
  const endTime = Date.now() + durationMs;
  while (Date.now() < endTime) {
    const sample = sampleGpu();
    if (sample !== null) samples.push(sample);
    await page.waitForTimeout(250);
  }
  return samples.length > 0 ? samples.reduce((a, b) => a + b, 0) / samples.length : 0;
}

// CSS property killers - each disables a GPU-intensive feature
const CSS_TESTS = [
  {
    name: "baseline",
    desc: "No changes",
    css: "",
  },
  {
    name: "no-animations",
    desc: "Disable all animations & transitions",
    css: `*, *::before, *::after {
      animation: none !important;
      animation-duration: 0s !important;
      transition: none !important;
      transition-duration: 0s !important;
    }`,
  },
  {
    name: "no-transforms",
    desc: "Disable all transforms",
    css: `*, *::before, *::after {
      transform: none !important;
    }`,
  },
  {
    name: "no-filters",
    desc: "Disable filter & backdrop-filter",
    css: `*, *::before, *::after {
      filter: none !important;
      backdrop-filter: none !important;
      -webkit-backdrop-filter: none !important;
    }`,
  },
  {
    name: "no-blur",
    desc: "Disable blur specifically",
    css: `*, *::before, *::after {
      filter: none !important;
      backdrop-filter: none !important;
      -webkit-backdrop-filter: none !important;
    }
    .glass, [class*="glass"] {
      background: rgba(0,0,0,0.8) !important;
    }`,
  },
  {
    name: "no-gradients",
    desc: "Replace gradients with solid colors",
    css: `*, *::before, *::after {
      background-image: none !important;
    }`,
  },
  {
    name: "no-shadows",
    desc: "Disable box-shadow & text-shadow",
    css: `*, *::before, *::after {
      box-shadow: none !important;
      text-shadow: none !important;
    }`,
  },
  {
    name: "no-opacity",
    desc: "Force full opacity",
    css: `*, *::before, *::after {
      opacity: 1 !important;
    }`,
  },
  {
    name: "no-will-change",
    desc: "Remove will-change hints",
    css: `*, *::before, *::after {
      will-change: auto !important;
    }`,
  },
  {
    name: "no-compositing",
    desc: "Force single layer (no GPU compositing)",
    css: `*, *::before, *::after {
      transform: none !important;
      will-change: auto !important;
      isolation: auto !important;
      mix-blend-mode: normal !important;
    }`,
  },
  {
    name: "NUKE-ALL",
    desc: "Kill everything GPU-related",
    css: `*, *::before, *::after {
      animation: none !important;
      transition: none !important;
      transform: none !important;
      filter: none !important;
      backdrop-filter: none !important;
      -webkit-backdrop-filter: none !important;
      box-shadow: none !important;
      text-shadow: none !important;
      opacity: 1 !important;
      will-change: auto !important;
      mix-blend-mode: normal !important;
      background-image: none !important;
    }`,
  },
];

async function main() {
  const args = process.argv.slice(2);
  let baseUrl = "http://localhost:30080";
  for (const arg of args) {
    if (arg.startsWith("--url=")) baseUrl = arg.slice(6);
  }

  console.log("\n==========================================");
  console.log("GPU CSS Property Diagnostic");
  console.log("==========================================");
  console.log(`URL: ${baseUrl}/?fx=none\n`);

  const browser = await chromium.launch({
    headless: false,
    args: ["--enable-gpu", "--enable-gpu-rasterization"],
  });

  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();

  // Reference
  await page.goto("about:blank");
  await page.waitForTimeout(2000);
  const blankGpu = await measureGpu(page, 3000);
  console.log(`Reference (about:blank): ${blankGpu.toFixed(1)}% GPU\n`);

  const results: { name: string; desc: string; gpu: number; delta: number }[] = [];
  let baselineGpu = 0;

  for (const test of CSS_TESTS) {
    await page.goto(`${baseUrl}/?fx=none`, { waitUntil: "networkidle" });
    await page.waitForTimeout(1500);

    if (test.css) {
      await page.addStyleTag({ content: test.css });
      await page.waitForTimeout(1000);
    }

    const gpu = await measureGpu(page, 4000);

    if (test.name === "baseline") {
      baselineGpu = gpu;
      console.log(`Baseline (no CSS changes): ${gpu.toFixed(1)}% GPU\n`);
      console.log("Testing CSS property disabling:\n");
    } else {
      const delta = baselineGpu - gpu;
      results.push({ name: test.name, desc: test.desc, gpu, delta });

      const indicator = delta > 5 ? "🔴" : delta > 2 ? "🟡" : "⚪";
      const verb = delta >= 0 ? "saves" : "costs";
      console.log(`  ${indicator} ${test.name.padEnd(18)} ${gpu.toFixed(1).padStart(5)}% GPU  (${verb} ${Math.abs(delta).toFixed(1)}%)  - ${test.desc}`);
    }
  }

  await browser.close();

  // Summary
  console.log("\n==========================================");
  console.log("SUMMARY");
  console.log("==========================================\n");

  const sorted = [...results].sort((a, b) => b.delta - a.delta);
  const significant = sorted.filter(r => r.delta > 2);

  if (significant.length > 0) {
    console.log("🎯 Significant GPU savers (>2%):\n");
    for (const r of significant) {
      console.log(`   ${r.name}: saves ${r.delta.toFixed(1)}% - ${r.desc}`);
    }
  } else {
    console.log("No single CSS property category saves >2% GPU.");
  }

  const nukeResult = results.find(r => r.name === "NUKE-ALL");
  if (nukeResult) {
    console.log(`\n📊 NUKE-ALL (disable everything): saves ${nukeResult.delta.toFixed(1)}% GPU`);
    console.log(`   Remaining GPU: ${nukeResult.gpu.toFixed(1)}% vs blank ${blankGpu.toFixed(1)}%`);
    console.log(`   Unexplained: ${(nukeResult.gpu - blankGpu).toFixed(1)}%`);
  }

  console.log("\n💡 If NUKE-ALL doesn't get close to blank, the GPU usage is from:");
  console.log("   - Page size/complexity (lots of DOM elements)");
  console.log("   - High refresh rate display (ProMotion 120Hz)");
  console.log("   - Chrome's base rendering overhead for this page");
  console.log("");
}

main().catch(console.error);
