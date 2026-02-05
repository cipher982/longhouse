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
import { mkdirSync, writeFileSync } from "fs";
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

type A11yFormat = "json" | "yaml" | "none";

interface CaptureResult {
  screenshotPath?: string;
  a11yPath?: string;
  a11yFormat: A11yFormat;
  error?: string;
}

function formatError(error: unknown): { message: string; detail: string } {
  if (error instanceof Error) {
    return { message: error.message, detail: error.stack ?? error.message };
  }
  const message = String(error);
  return { message, detail: message };
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
    const response = await fetch(`${backendUrl}/api/health`);
    return response.ok;
  } catch {
    return false;
  }
}

async function seedScene(scene: SceneName, backendUrl: string): Promise<void> {
  switch (scene) {
    case "empty":
      // Clear all sessions for true empty state
      try {
        const response = await fetch(`${backendUrl}/api/system/reset-sessions`, {
          method: "POST",
        });
        if (!response.ok) {
          console.warn(
            `  Warning: reset-sessions failed (${response.status} ${response.statusText})`
          );
        }
      } catch (error) {
        const { message } = formatError(error);
        console.warn(`  Warning: reset-sessions failed (${message})`);
      }
      break;
    case "demo":
      try {
        const response = await fetch(`${backendUrl}/api/system/seed-demo-sessions`, {
          method: "POST",
        });
        if (!response.ok) {
          console.warn(
            `  Warning: seed-demo-sessions failed (${response.status} ${response.statusText})`
          );
        }
      } catch (error) {
        const { message } = formatError(error);
        console.warn(`  Warning: seed-demo-sessions failed (${message})`);
      }
      break;
    case "onboarding-modal":
      // Reset user state to trigger onboarding (if endpoint exists)
      try {
        const response = await fetch(`${backendUrl}/api/system/reset-onboarding`, {
          method: "POST",
        });
        if (!response.ok) {
          console.warn(
            `  Warning: reset-onboarding failed (${response.status} ${response.statusText})`
          );
        }
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
  let a11yPath: string | undefined;
  let a11yFormat: A11yFormat = "none";
  try {
    const accessibilitySnapshot =
      (page as unknown as { accessibility?: { snapshot?: () => Promise<unknown> } }).accessibility
        ?.snapshot;
    if (typeof accessibilitySnapshot === "function") {
      const a11yTree = await accessibilitySnapshot();
      a11yPath = path.join(outputDir, `${pageName}-a11y.json`);
      writeFileSync(a11yPath, JSON.stringify(a11yTree, null, 2));
      a11yFormat = "json";
    } else {
      const ariaSnapshot = await page.locator("body").ariaSnapshot();
      a11yPath = path.join(outputDir, `${pageName}-a11y.yml`);
      writeFileSync(a11yPath, `${ariaSnapshot.trimEnd()}\n`);
      a11yFormat = "yaml";
    }
    console.log(`  A11y (${a11yFormat}): ${a11yPath}`);
  } catch (error) {
    const { message } = formatError(error);
    console.warn(`  Warning: a11y snapshot failed: ${message}`);
  }

  return { screenshotPath, a11yPath, a11yFormat };
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

  const consoleLogs: string[] = [];
  const errors: string[] = [];
  const artifacts: Record<string, CaptureResult> = {};
  const pagesToCapture = opts.all ? [...PAGES] : [opts.page];
  let tracePath: string | undefined;
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined;
  let context: BrowserContext | undefined;

  try {
    // Launch browser
    console.log("\nLaunching browser...");
    browser = await chromium.launch();
    context = await browser.newContext({
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

    console.log("\nCapturing pages...");
    for (const pageName of pagesToCapture) {
      console.log(`\n${pageName}:`);
      try {
        artifacts[pageName] = await captureBundle(
          context,
          page,
          pageName,
          outputDir,
          opts.baseUrl
        );
      } catch (error) {
        const { message, detail } = formatError(error);
        errors.push(`[${pageName}] ${message}`);
        consoleLogs.push(`[CAPTURE_ERROR] ${detail}`);
        artifacts[pageName] = { a11yFormat: "none", error: message };
      }
    }
  } catch (error) {
    const { message, detail } = formatError(error);
    errors.push(`[FATAL] ${message}`);
    consoleLogs.push(`[FATAL] ${detail}`);
  } finally {
    if (context && opts.trace) {
      tracePath = path.join(outputDir, "trace.zip");
      try {
        await context.tracing.stop({ path: tracePath });
        console.log(`\nTrace: ${tracePath}`);
      } catch (error) {
        const { message, detail } = formatError(error);
        errors.push(`[TRACE] ${message}`);
        consoleLogs.push(`[TRACE_ERROR] ${detail}`);
        tracePath = undefined;
      }
    }

    if (context) {
      await context.close();
    }
    if (browser) {
      await browser.close();
    }

    const consoleLogPath = path.join(outputDir, "console.log");
    writeFileSync(consoleLogPath, consoleLogs.join("\n"));
    console.log(`Console logs: ${consoleLogPath}`);

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
      errors,
      config: {
        baseUrl: opts.baseUrl,
        backendUrl: opts.backendUrl,
        viewport: { width: 1280, height: 720 },
      },
    };

    const manifestPath = path.join(outputDir, "manifest.json");
    writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));

    console.log(`\nManifest: ${manifestPath}`);
    if (tracePath) {
      console.log(`\nTo view trace: bunx playwright show-trace ${tracePath}`);
    }
    if (errors.length > 0) {
      console.error(`\nBundle complete with ${errors.length} error(s).`);
      process.exitCode = 1;
    } else {
      console.log("\nBundle complete!");
    }
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
