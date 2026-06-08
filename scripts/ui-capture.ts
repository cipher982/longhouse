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
 *   bunx tsx scripts/ui-capture.ts [page] [--scene=X] [--viewport=X] [--output=X] [--all] [--no-trace]
 *
 * Examples:
 *   bunx tsx scripts/ui-capture.ts timeline
 *   bunx tsx scripts/ui-capture.ts --scene=empty
 *   bunx tsx scripts/ui-capture.ts timeline --scene=timeline-card-stress --viewport=mobile
 *   bunx tsx scripts/ui-capture.ts session-detail --scene=session-detail-stress
 *   bunx tsx scripts/ui-capture.ts machines
 *   bunx tsx scripts/ui-capture.ts --all
 */

import { chromium, type BrowserContext, type Page } from "playwright";
import { execSync } from "child_process";
import { mkdirSync, writeFileSync } from "fs";
import path from "path";
import {
  buildSessionDetailStressFixture,
  SESSION_DETAIL_STRESS_NOW,
  SESSION_DETAIL_STRESS_SESSION_ID,
} from "./ui-fixtures/sessionDetailStress";
import { buildTimelineCardStressFixture } from "./ui-fixtures/timelineCardStress";

const PAGE_DEFINITIONS = {
  timeline: { path: "/timeline" },
  "session-detail": { path: `/timeline/${SESSION_DETAIL_STRESS_SESSION_ID}` },
  machines: { path: "/runners" },
  health: { path: "/health" },
  settings: { path: "/settings" },
  profile: { path: "/profile" },
  integrations: { path: "/settings/integrations" },
  devices: { path: "/settings/devices" },
  admin: { path: "/admin" },
} as const;
type PageName = keyof typeof PAGE_DEFINITIONS;
const PAGES = Object.keys(PAGE_DEFINITIONS) as PageName[];
const ALL_CAPTURE_PAGES = PAGES.filter((pageName) => pageName !== "session-detail");

const SCENES = [
  "empty",
  "demo",
  "onboarding-modal",
  "missing-api-key",
  "timeline-card-stress",
  "session-detail-stress",
] as const;
type SceneName = (typeof SCENES)[number];

const VIEWPORT_PRESETS = {
  desktop: {
    width: 1280,
    height: 720,
    isMobile: false,
    hasTouch: false,
    deviceScaleFactor: 1,
  },
  mobile: {
    width: 390,
    height: 844,
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 3,
  },
  "mobile-small": {
    width: 375,
    height: 667,
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 2,
  },
} as const;
type ViewportPresetName = keyof typeof VIEWPORT_PRESETS;
type ViewportConfig = {
  width: number;
  height: number;
  isMobile: boolean;
  hasTouch: boolean;
  deviceScaleFactor: number;
};

interface Options {
  page: PageName;
  scene: SceneName;
  output: string;
  baseUrl: string;
  backendUrl: string;
  trace: boolean;
  all: boolean;
  viewportName: string;
  viewport: ViewportConfig;
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
  const pageArg = args.find((a): a is PageName => {
    return !a.startsWith("--") && a in PAGE_DEFINITIONS;
  });

  // Parse named arguments
  const sceneArg = args
    .find((a) => a.startsWith("--scene="))
    ?.split("=")[1] as SceneName | undefined;
  const viewportArg = args.find((a) => a.startsWith("--viewport="))?.split("=")[1];
  const outputArg = args.find((a) => a.startsWith("--output="))?.split("=")[1];
  const noTrace = args.includes("--no-trace");
  const all = args.includes("--all");

  const timestamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const parsedViewport = parseViewport(viewportArg);

  return {
    page: (pageArg as PageName) || "timeline",
    scene: sceneArg || "demo",
    output: outputArg || `artifacts/ui-capture/${timestamp}`,
    baseUrl: process.env.FRONTEND_URL || "http://localhost:47200",
    backendUrl: process.env.BACKEND_URL || "http://localhost:47300",
    trace: !noTrace,
    all,
    viewportName: viewportArg || "desktop",
    viewport: parsedViewport,
  };
}

function parseViewport(value: string | undefined): ViewportConfig {
  if (!value || value === "desktop") {
    return { ...VIEWPORT_PRESETS.desktop };
  }

  if (value in VIEWPORT_PRESETS) {
    return { ...VIEWPORT_PRESETS[value as ViewportPresetName] };
  }

  const match = /^(\d+)x(\d+)$/.exec(value);
  if (!match) {
    throw new Error(
      `Unsupported viewport "${value}". Use one of ${Object.keys(VIEWPORT_PRESETS).join(", ")} or WIDTHxHEIGHT.`,
    );
  }

  const width = Number.parseInt(match[1], 10);
  const height = Number.parseInt(match[2], 10);

  return {
    width,
    height,
    isMobile: width <= 768,
    hasTouch: width <= 768,
    deviceScaleFactor: width <= 768 ? 3 : 1,
  };
}

function sceneUsesMockApi(scene: SceneName): boolean {
  return scene === "timeline-card-stress" || scene === "session-detail-stress";
}

function validateOptions(opts: Options): void {
  if (opts.page === "session-detail" && opts.scene !== "session-detail-stress") {
    throw new Error(
      "session-detail requires --scene=session-detail-stress (or PAGE=session-detail SCENE=session-detail-stress through make).",
    );
  }
  if (opts.all && opts.scene === "session-detail-stress") {
    throw new Error("SCENE=session-detail-stress captures PAGE=session-detail only; omit ALL=1.");
  }
}

function getPagesToCapture(opts: Options): PageName[] {
  if (!opts.all) {
    return [opts.page];
  }
  // Session detail needs a concrete session ID and fixture; keep it explicit
  // instead of smuggling a synthetic route into the generic app sweep.
  return [...ALL_CAPTURE_PAGES];
}

async function checkDevRunning(backendUrl: string): Promise<boolean> {
  try {
    const response = await fetch(`${backendUrl}/api/health`);
    return response.ok;
  } catch {
    return false;
  }
}

async function seedScene(
  scene: SceneName,
  backendUrl: string,
  pagesToCapture: PageName[],
): Promise<void> {
  if (sceneUsesMockApi(scene)) {
    return;
  }

  const capturesTimeline = pagesToCapture.includes("timeline");

  switch (scene) {
    case "empty":
      // Session seeding/resetting only applies to captures that include timeline.
      if (!capturesTimeline) return;
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
      if (!capturesTimeline) return;
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

async function installSceneMocks(
  context: BrowserContext,
  scene: SceneName,
  baseUrl: string,
): Promise<void> {
  if (!sceneUsesMockApi(scene)) {
    return;
  }

  const appOrigin = new URL(baseUrl).origin;

  if (scene === "session-detail-stress") {
    const fixture = buildSessionDetailStressFixture();
    const sessionBasePath = `/api/timeline/sessions/${fixture.session.id}`;

    await context.route(`${appOrigin}/api/**`, async (route) => {
      const pathname = new URL(route.request().url()).pathname;

      if (pathname === sessionBasePath) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(fixture.session),
        });
        return;
      }

      if (pathname === `${sessionBasePath}/thread`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(fixture.thread),
        });
        return;
      }

      if (pathname === `${sessionBasePath}/workspace`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(fixture.workspace),
        });
        return;
      }

      if (pathname === `${sessionBasePath}/projection`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(fixture.projection),
        });
        return;
      }

      if (pathname === `${sessionBasePath}/turns`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(fixture.turns),
        });
        return;
      }

      if (pathname === `${sessionBasePath}/workspace/stream`) {
        await route.fulfill({
          status: 204,
          body: "",
        });
        return;
      }

      if (pathname === `/api/sessions/${fixture.session.id}/lock`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ locked: false }),
        });
        return;
      }

      if (pathname === `/api/sessions/${fixture.session.id}/inputs`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: 9001,
              text: "Tighten the transcript rows first; the side pane can wait.",
              intent: "queue",
              status: "queued",
              created_at: "2026-04-15T16:10:50Z",
            },
          ]),
        });
        return;
      }

      // Dynamic-workflow run grouping (WorkflowRunsPanel).
      if (pathname === `/api/agents/sessions/${fixture.session.id}/workflows`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            session_id: fixture.session.id,
            workflow_runs: [{ workflow_run_id: "wf_deep_research", agent_count: 18, skill: "deep-research" }],
          }),
        });
        return;
      }

      if (pathname === "/api/agents/workflows/wf_deep_research") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            workflow_run_id: "wf_deep_research",
            skill: "deep-research",
            parent_session_id: fixture.session.id,
            agent_count: 18,
            agents: Array.from({ length: 18 }, (_v, i) => ({
              thread_id: `t-${i}`,
              session_id: fixture.session.id,
              is_primary: false,
              branch_kind: "subagent",
              agent_id: `a${(i + 1).toString(16).padStart(15, "0")}`,
              attribution_agent: "workflow-subagent",
              attribution_skill: "deep-research",
              source_path: null,
            })),
          }),
        });
        return;
      }

      await route.fallback();
    });
    return;
  }

  const fixture = buildTimelineCardStressFixture();

  await context.route(`${appOrigin}/api/**`, async (route) => {
    const pathname = new URL(route.request().url()).pathname;

    if (pathname === "/api/timeline/sessions") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(fixture.sessions),
      });
      return;
    }

    if (pathname === "/api/timeline/filters") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(fixture.filters),
      });
      return;
    }

    if (pathname === "/api/runners/" || pathname === "/api/runners") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(fixture.runners),
      });
      return;
    }

    if (pathname === "/api/timeline/sessions/stream") {
      await route.fulfill({
        status: 204,
        body: "",
      });
      return;
    }

    await route.fallback();
  });
}

async function installScenePageOverrides(page: Page, scene: SceneName, pageName: PageName): Promise<void> {
  if (pageName === "health") {
    await page.addInitScript(() => {
      Object.defineProperty(window, "__SINGLE_TENANT__", {
        configurable: true,
        value: true,
      });
    });
  }

  if (!sceneUsesMockApi(scene)) {
    return;
  }

  const fixtureNowIso = scene === "session-detail-stress" ? SESSION_DETAIL_STRESS_NOW : "2026-04-15T16:12:00Z";
  await page.addInitScript((nowIso) => {
    const fixtureNow = Date.parse(nowIso);
    Date.now = () => fixtureNow;
  }, fixtureNowIso);

  if (scene === "timeline-card-stress") {
    await page.addInitScript(() => {
      Object.defineProperty(window, "EventSource", {
        configurable: true,
        value: undefined,
      });
    });
  }
}

async function captureBundle(
  _context: BrowserContext,
  page: Page,
  pageName: PageName,
  outputDir: string,
  baseUrl: string,
  scene: SceneName,
): Promise<CaptureResult> {
  const url = `${baseUrl}${PAGE_DEFINITIONS[pageName].path}`;
  console.log(`  Navigating to ${url}...`);

  await installScenePageOverrides(page, scene, pageName);
  await page.goto(url);

  // Wait for page stability - prefer shared readiness flags.
  try {
    await page.waitForSelector("[data-screenshot-ready='true'], [data-ready='true']", { timeout: 5000 });
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
  validateOptions(opts);

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

  const pagesToCapture = getPagesToCapture(opts);

  // Seed scene
  console.log(`\nSeeding scene: ${opts.scene}`);
  await seedScene(opts.scene, opts.backendUrl, pagesToCapture);

  // Setup output directory
  const outputDir = opts.output;
  mkdirSync(outputDir, { recursive: true });
  console.log(`Output directory: ${outputDir}`);

  // Get git info
  const gitInfo = getGitInfo();

  const consoleLogs: string[] = [];
  const errors: string[] = [];
  const artifacts: Record<string, CaptureResult> = {};
  let tracePath: string | undefined;
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined;
  let context: BrowserContext | undefined;

  try {
    // Launch browser
    console.log("\nLaunching browser...");
    browser = await chromium.launch();
    context = await browser.newContext({
      viewport: { width: opts.viewport.width, height: opts.viewport.height },
      isMobile: opts.viewport.isMobile,
      hasTouch: opts.viewport.hasTouch,
      deviceScaleFactor: opts.viewport.deviceScaleFactor,
      reducedMotion: "reduce",
      timezoneId: "America/Los_Angeles",
      locale: "en-US",
    });

    await installSceneMocks(context, opts.scene, opts.baseUrl);

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
          opts.baseUrl,
          opts.scene,
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
        viewport_name: opts.viewportName,
        viewport: opts.viewport,
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
