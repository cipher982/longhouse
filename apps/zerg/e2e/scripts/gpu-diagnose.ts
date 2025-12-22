#!/usr/bin/env bun
/**
 * GPU Usage Diagnostic - Find what's eating GPU on the landing page
 *
 * Systematically hides page sections and measures GPU to identify culprits.
 *
 * Usage:
 *   bun run scripts/gpu-diagnose.ts [--url=http://localhost:30080]
 */

import { chromium, type Browser, type Page } from "playwright";
import { execSync } from "child_process";

interface GpuSample {
  deviceUtilization: number;
  rendererUtilization: number;
}

function sampleGpu(): GpuSample | null {
  try {
    const output = execSync(
      'ioreg -r -d 1 -c IOAccelerator 2>/dev/null | grep "PerformanceStatistics"',
      { encoding: "utf-8", timeout: 2000 }
    );
    const match = output.match(/PerformanceStatistics.*?\{([^}]+)\}/);
    if (!match) return null;
    const statsStr = match[1];
    const getNum = (key: string): number => {
      const m = statsStr.match(new RegExp(`"${key}"=(\\d+)`));
      return m ? parseInt(m[1], 10) : 0;
    };
    return {
      deviceUtilization: getNum("Device Utilization %"),
      rendererUtilization: getNum("Renderer Utilization %"),
    };
  } catch {
    return null;
  }
}

async function measureGpu(page: Page, durationMs: number, intervalMs: number = 300): Promise<number> {
  const samples: number[] = [];
  const endTime = Date.now() + durationMs;
  while (Date.now() < endTime) {
    const sample = sampleGpu();
    if (sample) samples.push(sample.deviceUtilization);
    await page.waitForTimeout(intervalMs);
  }
  return samples.length > 0 ? samples.reduce((a, b) => a + b, 0) / samples.length : 0;
}

// CSS selectors for major page sections to test
const PAGE_SECTIONS = [
  { name: "particle-bg", selector: ".particle-bg", desc: "Particle background layer" },
  { name: "glow-orb", selector: ".landing-glow-orb", desc: "Gradient orb background" },
  { name: "hero-section", selector: ".hero-section", desc: "Hero section (includes SVG)" },
  { name: "hero-visual", selector: ".hero-visual", desc: "Hero SVG animation only" },
  { name: "hero-visual svg", selector: ".hero-visual svg", desc: "Hero SVG element" },
  { name: "pas-section", selector: ".pas-section", desc: "Problem-Agitate-Solve section" },
  { name: "scenarios-section", selector: ".scenarios-section", desc: "Scenarios section" },
  { name: "differentiation", selector: ".differentiation-section", desc: "Differentiation section" },
  { name: "nerd-section", selector: ".nerd-section", desc: "Technical details section" },
  { name: "integrations", selector: ".integrations-section", desc: "Integrations section" },
  { name: "trust-section", selector: ".trust-section", desc: "Trust/social proof section" },
  { name: "footer-cta", selector: ".footer-cta", desc: "Footer CTA section" },
  { name: "all-animations", selector: "*", desc: "Kill ALL CSS animations", css: "animation: none !important; transition: none !important;" },
];

async function main() {
  const args = process.argv.slice(2);
  let baseUrl = "http://localhost:30080";
  for (const arg of args) {
    if (arg.startsWith("--url=")) baseUrl = arg.slice(6);
  }

  console.log("\n==========================================");
  console.log("GPU Usage Diagnostic");
  console.log("==========================================");
  console.log(`URL: ${baseUrl}/?fx=none`);
  console.log("\nThis test hides page sections one-by-one to find GPU culprits.\n");

  // Test ioreg
  const testSample = sampleGpu();
  if (!testSample) {
    console.error("ERROR: Cannot read GPU stats. macOS with Apple Silicon required.");
    process.exit(1);
  }

  const browser = await chromium.launch({
    headless: false,
    args: ["--enable-gpu", "--enable-gpu-rasterization"],
  });

  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();

  // Baseline: blank page
  console.log("--- Reference Baselines ---\n");
  await page.goto("about:blank");
  await page.waitForTimeout(2000);
  const blankGpu = await measureGpu(page, 4000);
  console.log(`  about:blank:        ${blankGpu.toFixed(1)}% GPU`);

  // Baseline: landing page with fx=none (no injected CSS)
  await page.goto(`${baseUrl}/?fx=none`, { waitUntil: "networkidle" });
  await page.waitForTimeout(3000);
  const baselineGpu = await measureGpu(page, 5000);
  console.log(`  Landing (fx=none):  ${baselineGpu.toFixed(1)}% GPU`);
  console.log(`  Delta from blank:   +${(baselineGpu - blankGpu).toFixed(1)}%`);

  // Test each section by hiding it
  console.log("\n--- Section-by-Section Analysis ---\n");
  console.log("(Lower GPU when hidden = that section costs GPU)\n");

  const results: { name: string; desc: string; gpu: number; delta: number }[] = [];

  for (const section of PAGE_SECTIONS) {
    // Reload page fresh
    await page.goto(`${baseUrl}/?fx=none`, { waitUntil: "networkidle" });
    await page.waitForTimeout(1500);

    // Inject CSS to hide/disable the section
    const css = section.css
      ? `${section.selector} { ${section.css} }`
      : `${section.selector} { display: none !important; visibility: hidden !important; }`;

    await page.addStyleTag({ content: css });
    await page.waitForTimeout(1500);

    const gpu = await measureGpu(page, 4000);
    const delta = baselineGpu - gpu; // Positive = this section was costing GPU
    results.push({ name: section.name, desc: section.desc, gpu, delta });

    const indicator = delta > 5 ? "🔴" : delta > 2 ? "🟡" : "⚪";
    console.log(`  ${indicator} Hide ${section.name.padEnd(18)} → ${gpu.toFixed(1)}% GPU  (${delta >= 0 ? "saves" : "adds"} ${Math.abs(delta).toFixed(1)}%)`);
  }

  await browser.close();

  // Summary
  console.log("\n==========================================");
  console.log("SUMMARY - GPU Cost by Section");
  console.log("==========================================\n");

  const sorted = [...results].sort((a, b) => b.delta - a.delta);

  console.log("Sections sorted by GPU cost (hiding saves most GPU first):\n");
  for (const r of sorted) {
    if (r.delta > 1) {
      const bar = "█".repeat(Math.min(20, Math.round(r.delta)));
      console.log(`  ${r.name.padEnd(20)} ${bar} ${r.delta.toFixed(1)}% saved`);
    }
  }

  const topCulprits = sorted.filter(r => r.delta > 3);
  if (topCulprits.length > 0) {
    console.log("\n🎯 Top GPU culprits (>3% savings when hidden):");
    for (const c of topCulprits) {
      console.log(`   - ${c.name}: ${c.desc}`);
    }
  } else {
    console.log("\n✅ No single section accounts for >3% GPU.");
    console.log("   The cost may be spread across many small elements,");
    console.log("   or caused by the page's base rendering at high refresh rate.");
  }

  console.log("\n💡 Next steps:");
  console.log("   1. Inspect culprit elements in Chrome DevTools → Layers panel");
  console.log("   2. Enable Rendering → Paint Flashing to see live repaints");
  console.log("   3. Check for CSS animations not gated by data-fx-* attributes");
  console.log("");
}

main().catch((err) => {
  console.error("Error:", err);
  process.exit(1);
});
