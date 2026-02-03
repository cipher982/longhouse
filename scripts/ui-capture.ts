#!/usr/bin/env bun
/**
 * UI Capture Script - Debug bundle generator for agent workflows
 *
 * Produces a debug bundle, not just a screenshot:
 * - Screenshots (page + key components)
 * - Playwright trace (time-travel debugging)
 * - Console logs + network failures
 * - Accessibility snapshot (JSON)
 * - Manifest.json with metadata
 *
 * Usage:
 *   bunx tsx scripts/ui-capture.ts [page] [--scene=X] [--output=X] [--all] [--no-trace]
 *
 * Examples:
 *   bunx tsx scripts/ui-capture.ts timeline
 *   bunx tsx scripts/ui-capture.ts --scene=empty
 *   bunx tsx scripts/ui-capture.ts --all
 */

import { chromium, type BrowserContext, type Page } from "playwright";
import { execSync } from "child_process";
import { mkdirSync, writeFileSync, existsSync } from "fs";
import path from "path";

const PAGES = ["timeline", "chat", "dashboard", "settings"] as const;
type PageName = (typeof PAGES)[number];

const SCENES = ["empty", "demo", "onboarding-modal", "missing-api-key"] as const;
type SceneName = (typeof SCENES)[number];

interface Options {
  page: PageName;
  scene: SceneName;
  output: string;
  baseUrl: string;
  backendUrl: string;
  trace: boolean;
  all: boolean;
}

interface CaptureResult {
  screenshotPath: string;
  a11yPath: string;
}

function parseArgs(): Options {
  const args = process.argv.slice(2);

  // Find page argument (positional, not prefixed with --)
  const pageArg = args.find(
    (a) => !a.startsWith("--") && PAGES.includes(a as PageName)
  );

  // Parse named arguments
  const sceneArg = args
    .find((a) => a.startsWith("--scene="))
    ?.split("=")[1] as SceneName | undefined;
  const outputArg = args.find((a) => a.startsWith("--output="))?.split("=")[1];
  const noTrace = args.includes("--no-trace");
  const all = args.includes("--all");

  const timestamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);

  return {
    page: (pageArg as PageName) || "timeline",
    scene: sceneArg || "demo",
    output: outputArg || `artifacts/ui-capture/${timestamp}`,
    baseUrl: process.env.FRONTEND_URL || "http://localhost:47200",
    backendUrl: process.env.BACKEND_URL || "http://localhost:47300",
    trace: !noTrace,
    all,
  };
}

async function checkDevRunning(backendUrl: string): Promise<boolean> {
  try {
    const response = await fetch(`${backendUrl}/health`);
    return response.ok;
  } catch {
    return false;
  }
}

async function seedScene(scene: SceneName, backendUrl: string): Promise<void> {
  switch (scene) {
    case "empty":
      // No-op: empty state
      break;
    case "demo":
      await fetch(`${backendUrl}/api/system/seed-demo-sessions`, {
        method: "POST",
      });
      break;
    case "onboarding-modal":
      // Reset user state to trigger onboarding (if endpoint exists)
      try {
        await fetch(`${backendUrl}/api/system/reset-onboarding`, {
          method: "POST",
        });
      } catch {
        console.warn("  Warning: reset-onboarding endpoint not available");
      }
      break;
    case "missing-api-key":
      // This scene relies on no API key being configured
      // In dev mode, we can't easily remove keys, so this is best-effort
      break;
  }
}

async function captureBundle(
  context: BrowserContext,
  page: Page,
  pageName: PageName,
  outputDir: string,
  baseUrl: string
): Promise<CaptureResult> {
  const url = `${baseUrl}/${pageName}`;
  console.log(`  Navigating to ${url}...`);

  await page.goto(url);

  // Wait for page stability - try data-page-ready first, then networkidle
  try {
    await page.waitForSelector("[data-page-ready]", { timeout: 5000 });
  } catch {
    await page.waitForLoadState("networkidle", { timeout: 10000 });
  }

  // Inject CSS to kill animations for deterministic screenshots
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        transition: none !important;
        animation: none !important;
        animation-delay: 0s !important;
        animation-duration: 0s !important;
        caret-color: transparent !important;
      }
    `,
  });

  // Let CSS apply
  await page.waitForTimeout(100);

  // Capture screenshot
  const screenshotPath = path.join(outputDir, `${pageName}.png`);
  await page.screenshot({ path: screenshotPath, fullPage: false });
  console.log(`  Screenshot: ${screenshotPath}`);

  // Capture accessibility snapshot
  const a11yPath = path.join(outputDir, `${pageName}-a11y.json`);
  const a11yTree = await page.accessibility.snapshot();
  writeFileSync(a11yPath, JSON.stringify(a11yTree, null, 2));
  console.log(`  A11y tree: ${a11yPath}`);

  return { screenshotPath, a11yPath };
}

function getGitInfo(): { sha: string; branch: string; dirty: boolean } {
  try {
    const sha = execSync("git rev-parse --short HEAD", { encoding: "utf-8" }).trim();
    const branch = execSync("git rev-parse --abbrev-ref HEAD", { encoding: "utf-8" }).trim();
    const status = execSync("git status --porcelain", { encoding: "utf-8" });
    return { sha, branch, dirty: status.length > 0 };
  } catch {
    return { sha: "unknown", branch: "unknown", dirty: false };
  }
}

async function main() {
  const opts = parseArgs();

  console.log("UI Capture - Debug Bundle Generator");
  console.log("====================================\n");

  // Check if dev is running
  const backendUp = await checkDevRunning(opts.backendUrl);
  if (!backendUp) {
    console.error(`Dev server not running at ${opts.backendUrl}`);
    console.error("Start with: make dev");
    process.exit(1);
  }
  console.log(`Backend healthy at ${opts.backendUrl}`);

  // Seed scene
  console.log(`\nSeeding scene: ${opts.scene}`);
  await seedScene(opts.scene, opts.backendUrl);

  // Setup output directory
  const outputDir = opts.output;
  mkdirSync(outputDir, { recursive: true });
  console.log(`Output directory: ${outputDir}`);

  // Get git info
  const gitInfo = getGitInfo();

  // Launch browser
  console.log("\nLaunching browser...");
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    reducedMotion: "reduce",
    timezoneId: "America/Los_Angeles",
    locale: "en-US",
  });

  // Start tracing if enabled
  if (opts.trace) {
    await context.tracing.start({ screenshots: true, snapshots: true });
  }

  const page = await context.newPage();

  // Capture console logs
  const consoleLogs: string[] = [];
  page.on("console", (msg) => {
    const text = `[${msg.type().toUpperCase()}] ${msg.text()}`;
    consoleLogs.push(text);
  });

  // Capture page errors
  page.on("pageerror", (err) => {
    consoleLogs.push(`[PAGE_ERROR] ${err.message}`);
  });

  // Capture request failures
  page.on("requestfailed", (request) => {
    consoleLogs.push(
      `[REQUEST_FAILED] ${request.method()} ${request.url()} - ${request.failure()?.errorText}`
    );
  });

  // Determine which pages to capture
  const pagesToCapture = opts.all ? [...PAGES] : [opts.page];
  const artifacts: Record<string, CaptureResult> = {};

  console.log("\nCapturing pages...");
  for (const pageName of pagesToCapture) {
    console.log(`\n${pageName}:`);
    artifacts[pageName] = await captureBundle(
      context,
      page,
      pageName,
      outputDir,
      opts.baseUrl
    );
  }

  // Stop tracing
  let tracePath: string | undefined;
  if (opts.trace) {
    tracePath = path.join(outputDir, "trace.zip");
    await context.tracing.stop({ path: tracePath });
    console.log(`\nTrace: ${tracePath}`);
  }

  // Write console logs
  const consoleLogPath = path.join(outputDir, "console.log");
  writeFileSync(consoleLogPath, consoleLogs.join("\n"));
  console.log(`Console logs: ${consoleLogPath}`);

  // Write manifest
  const manifest = {
    timestamp: new Date().toISOString(),
    git: gitInfo,
    scene: opts.scene,
    pages: pagesToCapture,
    artifacts: {
      ...artifacts,
      trace: tracePath,
      console: consoleLogPath,
    },
    config: {
      baseUrl: opts.baseUrl,
      backendUrl: opts.backendUrl,
      viewport: { width: 1280, height: 720 },
    },
  };

  const manifestPath = path.join(outputDir, "manifest.json");
  writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));

  console.log(`\nManifest: ${manifestPath}`);
  console.log("\nBundle complete!");
  console.log(`\nTo view trace: npx playwright show-trace ${tracePath}`);

  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
