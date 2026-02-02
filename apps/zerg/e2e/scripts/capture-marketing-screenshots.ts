#!/usr/bin/env bun
/**
 * Marketing Screenshot Capture Script
 *
 * Hydrates demo data and captures screenshots for README/landing page.
 * Repeatable, version-controlled, and consistent.
 *
 * Usage:
 *   bun run apps/zerg/e2e/scripts/capture-marketing-screenshots.ts [options]
 *
 * Options:
 *   --url=<base>        Base URL (default: http://localhost:47200)
 *   --output=<dir>      Output directory (default: apps/zerg/frontend-web/branding)
 *   --headless          Run headless (default: false for accurate rendering)
 *   --seed-demo         Seed demo data via API before capture (default: true)
 */

import { chromium, type Browser, type Page } from "playwright";
import * as fs from "fs";
import * as path from "path";

interface CaptureOptions {
  baseUrl: string;
  outputDir: string;
  headless: boolean;
  seedDemo: boolean;
}

interface Screenshot {
  name: string;
  path: string;
  url: string;
  viewport: { width: number; height: number };
  waitFor?: string | number;
  setup?: (page: Page) => Promise<void>;
  description: string;
}

function parseArgs(): CaptureOptions {
  const args = process.argv.slice(2);
  let baseUrl = "http://localhost:47200";
  let outputDir = "apps/zerg/frontend-web/branding";
  let headless = false;
  let seedDemo = true;

  for (const arg of args) {
    if (arg.startsWith("--url=")) {
      baseUrl = arg.slice(6);
    } else if (arg.startsWith("--output=")) {
      outputDir = arg.slice(9);
    } else if (arg === "--headless") {
      headless = true;
    } else if (arg === "--no-seed") {
      seedDemo = false;
    }
  }

  return { baseUrl, outputDir, headless, seedDemo };
}

async function seedDemoData(baseUrl: string): Promise<void> {
  console.log("  Seeding demo data via API...");

  // Call demo data seed endpoint (you'll need to add this)
  const response = await fetch(`${baseUrl}/api/admin/seed-demo-data`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });

  if (!response.ok) {
    console.warn(`  Warning: Demo seed failed with ${response.status}`);
    console.warn("  Continuing anyway - screenshots may show empty state");
  } else {
    console.log("  ✓ Demo data seeded");
  }
}

async function captureScreenshot(
  page: Page,
  screenshot: Screenshot,
  outputDir: string
): Promise<void> {
  console.log(`  Capturing: ${screenshot.name}`);

  // Set viewport
  await page.setViewportSize(screenshot.viewport);

  // Navigate
  await page.goto(screenshot.url, { waitUntil: "networkidle" });

  // Wait if specified
  if (screenshot.waitFor) {
    if (typeof screenshot.waitFor === "string") {
      await page.waitForSelector(screenshot.waitFor, { timeout: 10000 });
    } else {
      await page.waitForTimeout(screenshot.waitFor);
    }
  }

  // Run setup if specified
  if (screenshot.setup) {
    await screenshot.setup(page);
  }

  // Give layout time to settle
  await page.waitForTimeout(500);

  // Disable animations for consistent screenshots
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation-duration: 0s !important;
        animation-delay: 0s !important;
        transition-duration: 0s !important;
        transition-delay: 0s !important;
      }
    `,
  });

  // Take screenshot
  const filePath = path.join(outputDir, screenshot.path);
  await page.screenshot({
    path: filePath,
    fullPage: false,
  });

  console.log(`    → ${filePath}`);
}

async function main() {
  const options = parseArgs();

  console.log("\n========================================");
  console.log("Marketing Screenshot Capture");
  console.log("========================================");
  console.log(`Base URL:    ${options.baseUrl}`);
  console.log(`Output Dir:  ${options.outputDir}`);
  console.log(`Headless:    ${options.headless}`);
  console.log(`Seed Demo:   ${options.seedDemo}`);

  // Create output directory
  fs.mkdirSync(options.outputDir, { recursive: true });

  // Seed demo data if requested
  if (options.seedDemo) {
    await seedDemoData(options.baseUrl);
  }

  // Define screenshots to capture
  const screenshots: Screenshot[] = [
    {
      name: "Timeline Hero",
      path: "timeline-screenshot.png",
      url: `${options.baseUrl}/timeline`,
      viewport: { width: 1920, height: 1080 },
      waitFor: "[data-testid='timeline-sessions']",
      description: "Main timeline view with sessions",
    },
    {
      name: "Session Detail",
      path: "session-detail-screenshot.png",
      url: `${options.baseUrl}/timeline`,
      viewport: { width: 1920, height: 1080 },
      waitFor: "[data-testid='timeline-sessions']",
      setup: async (page) => {
        // Click first session to open detail view
        const firstSession = page.locator("[data-testid='session-card']").first();
        if (await firstSession.count() > 0) {
          await firstSession.click();
          await page.waitForTimeout(500);
        }
      },
      description: "Session detail with messages and tool calls",
    },
    {
      name: "Search",
      path: "search-screenshot.png",
      url: `${options.baseUrl}/timeline`,
      viewport: { width: 1920, height: 1080 },
      waitFor: "[data-testid='timeline-sessions']",
      setup: async (page) => {
        // Open search and type query
        const searchInput = page.locator("[data-testid='search-input']");
        if (await searchInput.count() > 0) {
          await searchInput.fill("authentication");
          await page.waitForTimeout(500);
        }
      },
      description: "Search in action filtering sessions",
    },
    {
      name: "Landing Page",
      path: "landing-hero.png",
      url: `${options.baseUrl}/`,
      viewport: { width: 1920, height: 1080 },
      waitFor: 1000,
      description: "Landing page hero section",
    },
  ];

  // Launch browser
  console.log("\nLaunching browser...\n");
  const browser = await chromium.launch({
    headless: options.headless,
  });

  try {
    const context = await browser.newContext({
      viewport: { width: 1920, height: 1080 },
    });
    const page = await context.newPage();

    // Capture each screenshot
    for (const screenshot of screenshots) {
      await captureScreenshot(page, screenshot, options.outputDir);
    }

    await context.close();
  } finally {
    await browser.close();
  }

  // Generate README snippet
  console.log("\n========================================");
  console.log("README Snippet");
  console.log("========================================\n");
  console.log("Add this to your README.md:\n");
  console.log("```markdown");
  console.log("## What It Looks Like\n");
  for (const screenshot of screenshots) {
    console.log(`### ${screenshot.name}`);
    console.log(`${screenshot.description}\n`);
    console.log(`![${screenshot.name}](${screenshot.path})\n`);
  }
  console.log("```");

  console.log("\n✓ Done! Screenshots saved to", options.outputDir);
}

main().catch((err) => {
  console.error("Error:", err);
  process.exit(1);
});
