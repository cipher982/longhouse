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
 * Nothing needs to be running first: fixture scenes answer every API call from
 * Playwright routes, and the script starts Vite itself (and stops it) when
 * nothing is listening on FRONTEND_URL. Demo-data scenes still need the backend.
 *
 * Usage:
 *   bunx tsx scripts/ui-capture.ts [page] [--scene=X] [--viewport=X] [--output=X] [--all] [--no-trace] [--probe=sel1,sel2]
 *
 * --probe writes <page>-probe.json with the bounding box and key computed
 * styles of each selector (first match), so a layout can be measured, not
 * just eyeballed.
 *
 * Examples:
 *   bunx tsx scripts/ui-capture.ts timeline
 *   bunx tsx scripts/ui-capture.ts --scene=empty
 *   bunx tsx scripts/ui-capture.ts timeline --scene=timeline-card-stress --viewport=mobile
 *   bunx tsx scripts/ui-capture.ts session-detail --scene=session-detail-stress
 *   bunx tsx scripts/ui-capture.ts session-detail --scene=session-resume
 *   bunx tsx scripts/ui-capture.ts session-detail --scene=session-tones   # one PNG per composer tone
 *   bunx tsx scripts/ui-capture.ts machines
 *   bunx tsx scripts/ui-capture.ts --all
 */

import { chromium, type BrowserContext, type Page, type Route } from "playwright";
import { execSync, spawn } from "child_process";
import { mkdirSync, writeFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import {
  buildSessionDetailStressFixture,
  buildSessionQuestionFixture,
  buildSessionAttentionFixture,
  buildSessionResumeFixture,
  buildSessionStaleObservationFixture,
  buildSessionToneFixture,
  SESSION_DETAIL_STRESS_NOW,
  SESSION_DETAIL_STRESS_SESSION_ID,
  SESSION_TONES,
  type SessionTone,
} from "./ui-fixtures/sessionDetailStress";
import { buildTimelineCardStressFixture } from "./ui-fixtures/timelineCardStress";
import {
  LANDING_SEARCH_QUERY,
  buildLandingSessionFixture,
  buildLandingTimelineFixture,
} from "./ui-fixtures/landingShowcase";

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
  "launch-unavailable",
  "launch-model-picker",
  "session-detail-stress",
  "session-question",
  "session-attention",
  "session-resume",
  "session-stale-observation",
  "session-tones",
  "landing",
  "landing-search",
  "landing-session",
] as const;
type SceneName = (typeof SCENES)[number];

/** Curated landing-page showcase data (scripts/ui-fixtures/landingShowcase.ts). */
const LANDING_TIMELINE_SCENES: readonly SceneName[] = ["landing", "landing-search"];
const LANDING_SCENES: readonly SceneName[] = [...LANDING_TIMELINE_SCENES, "landing-session"];

const SESSION_DETAIL_SCENES: readonly SceneName[] = [
  "landing-session",
  "session-detail-stress",
  "session-question",
  "session-attention",
  "session-resume",
  "session-stale-observation",
  "session-tones",
];

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
  probe: string[];
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
  const probeArg = args.find((a) => a.startsWith("--probe="))?.slice("--probe=".length);
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
    probe: probeArg ? probeArg.split(",").map((s) => s.trim()).filter(Boolean) : [],
  };
}

function parseViewport(value: string | undefined): ViewportConfig {
  if (!value || value === "desktop") {
    return { ...VIEWPORT_PRESETS.desktop };
  }

  if (value in VIEWPORT_PRESETS) {
    return { ...VIEWPORT_PRESETS[value as ViewportPresetName] };
  }

  const match = /^(\d+)x(\d+)(?:@(\d+))?$/.exec(value);
  if (!match) {
    throw new Error(
      `Unsupported viewport "${value}". Use one of ${Object.keys(VIEWPORT_PRESETS).join(", ")}, WIDTHxHEIGHT, or WIDTHxHEIGHT@SCALE.`,
    );
  }

  const width = Number.parseInt(match[1], 10);
  const height = Number.parseInt(match[2], 10);
  if (match[3]) {
    return { width, height, isMobile: false, hasTouch: false, deviceScaleFactor: Number.parseInt(match[3], 10) };
  }

  return {
    width,
    height,
    isMobile: width <= 768,
    hasTouch: width <= 768,
    deviceScaleFactor: width <= 768 ? 3 : 1,
  };
}

function sceneUsesMockApi(scene: SceneName): boolean {
  return (
    scene === "timeline-card-stress" ||
    scene === "launch-unavailable" ||
    scene === "launch-model-picker" ||
    LANDING_TIMELINE_SCENES.includes(scene) ||
    scene === "landing-session" ||
    scene === "session-detail-stress" ||
    scene === "session-question" ||
    scene === "session-attention" ||
    scene === "session-resume" ||
    scene === "session-stale-observation" ||
    scene === "session-tones"
  );
}

function validateOptions(opts: Options): void {
  if (opts.page === "session-detail" && !SESSION_DETAIL_SCENES.includes(opts.scene)) {
    throw new Error(`session-detail requires one of: ${SESSION_DETAIL_SCENES.map((s) => `--scene=${s}`).join(", ")}.`);
  }
  if (SESSION_DETAIL_SCENES.includes(opts.scene) && opts.page !== "session-detail") {
    throw new Error(`--scene=${opts.scene} captures PAGE=session-detail only.`);
  }
  if (opts.all && SESSION_DETAIL_SCENES.includes(opts.scene)) {
    throw new Error("Session-detail scenes capture PAGE=session-detail only; omit ALL=1.");
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
  tone: SessionTone = "running",
): Promise<void> {
  if (!sceneUsesMockApi(scene)) {
    return;
  }

  const appOrigin = new URL(baseUrl).origin;
  // Tone scenes re-install per frame; drop the previous handler first.
  await context.unroute(`${appOrigin}/api/**`);

  if (SESSION_DETAIL_SCENES.includes(scene)) {
    const fixture =
      scene === "landing-session"
        ? buildLandingSessionFixture()
        : scene === "session-resume"
        ? buildSessionResumeFixture()
        : scene === "session-question"
          ? buildSessionQuestionFixture()
          : scene === "session-attention"
          ? buildSessionAttentionFixture()
          : scene === "session-stale-observation"
          ? buildSessionStaleObservationFixture()
          : scene === "session-tones"
            ? buildSessionToneFixture(tone)
            : buildSessionDetailStressFixture();
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

      if (pathname === `${sessionBasePath}/resume-intent`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            session_id: fixture.session.id,
            provider: "codex",
            machine_id: "device-cinder",
            machine_label: "cinder",
            cwd: "/Users/example/git/zerg",
            available: true,
            reason: null,
            argv: ["longhouse", "codex", "--cwd", "/Users/example/git/zerg", "--resume-session", fixture.session.id],
            command: `longhouse codex --cwd /Users/example/git/zerg --resume-session ${fixture.session.id}`,
            handoff: "terminal_command",
          }),
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

      if (pathname === `/api/sessions/${fixture.session.id}/inputs` && scene === "landing-session") {
        await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
        return;
      }

      if (pathname === `/api/sessions/${fixture.session.id}/inputs`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            // The stress scene shows every outbox state the transcript tail
            // renders; other scenes keep the single queued row.
            ...(scene === "session-detail-stress"
              ? [
                  {
                    id: 8999,
                    text: "Run the migration against the staging copy too.",
                    intent: "auto",
                    status: "failed",
                    last_error: "provider rejected the input",
                    created_at: "2026-04-15T16:10:30Z",
                  },
                  {
                    id: 9000,
                    text: "tldr i missed this whole thread?",
                    intent: "auto",
                    status: "delivering",
                    created_at: "2026-04-15T16:10:40Z",
                  },
                ]
              : []),
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

      // Dynamic-workflow run grouping (WorkflowRunsPanel) — browser-auth timeline routes.
      if (pathname === `${sessionBasePath}/workflows`) {
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

      if (pathname === "/api/timeline/workflows/wf_deep_research") {
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

      await sealOrFallback(route, scene, pathname);
    });
    return;
  }

  const fixture = buildTimelineCardStressFixture();

  await context.route(`${appOrigin}/api/**`, async (route) => {
    const requestUrl = new URL(route.request().url());
    const pathname = requestUrl.pathname;

    if (pathname === "/api/timeline/sessions") {
      const sessions = LANDING_TIMELINE_SCENES.includes(scene)
        ? buildLandingTimelineFixture(requestUrl.searchParams.get("query") ?? "").sessions
        : fixture.sessions;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(sessions),
      });
      return;
    }

    if (pathname === "/api/timeline/filters") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          LANDING_TIMELINE_SCENES.includes(scene) ? buildLandingTimelineFixture().filters : fixture.filters,
        ),
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

    if (scene === "launch-unavailable" && pathname.endsWith("/providers/codex/sign-in")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          attempt_id: "fixture-attempt",
          provider: "codex",
          flow: "device_code",
          verification_url: "https://auth.openai.com/codex/device",
          user_code: "VWSN-8F9KZ",
          prerequisite: "Enable device code authorization in ChatGPT > Settings > Security first.",
          expires_in_secs: 900,
        }),
      });
      return;
    }

    if (scene === "launch-unavailable" && pathname === "/api/timeline/machines") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(LAUNCH_UNAVAILABLE_MACHINES) });
      return;
    }

    if (scene === "launch-unavailable" && pathname.startsWith("/api/timeline/machines/") && pathname.endsWith("/workspaces")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          device_id: "workbench",
          workspaces: [{ path: "/Users/you/git/longhouse", label: "longhouse", git_repo: null, last_used_at: null, session_count: 3 }],
        }),
      });
      return;
    }

    if (scene === "launch-model-picker" && pathname === "/api/timeline/machines") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(LAUNCH_MODEL_PICKER_MACHINES) });
      return;
    }

    if (scene === "launch-model-picker" && pathname.startsWith("/api/timeline/machines/") && pathname.endsWith("/workspaces")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          device_id: "workbench",
          workspaces: [{ path: "/Users/you/git/longhouse", label: "longhouse", git_repo: null, last_used_at: null, session_count: 3 }],
        }),
      });
      return;
    }

    if (scene === "launch-model-picker" && pathname.includes("/providers/") && pathname.endsWith("/models")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          device_id: "workbench",
          provider: "codex",
          days_back: 90,
          models: [
            { model: "gpt-5.6-luna", last_used_at: hoursBeforeFixtureNow(2) },
            { model: "gpt-5.6-sol", last_used_at: hoursBeforeFixtureNow(26) },
            { model: "gpt-5.5", last_used_at: hoursBeforeFixtureNow(24 * 5) },
          ],
        }),
      });
      return;
    }

    await sealOrFallback(route, scene, pathname);
  });
}

// A remote workbench where OMP can run but Claude and Codex are signed out:
// the launcher must offer only OMP and say what to fix for the others.
const LAUNCH_UNAVAILABLE_MACHINES = {
  machines: [
    {
      device_id: "workbench",
      machine_name: "workbench",
      online: true,
      control_channel_status: "connected",
      supports: ["claude.turn_start", "codex.turn_start", "omp.turn_start", "claude.sign_in", "codex.sign_in"],
      control_operations_by_provider: { claude: ["turn_start"], codex: ["turn_start"], omp: ["turn_start"] },
      last_seen_at: "2026-04-15T16:11:00Z",
      connected_since: "2026-04-15T12:00:00Z",
      engine_build: "fixture",
      launch: {
        blocked_by: null,
        providers: [{ provider: "omp" }],
        default_provider: "omp",
        unavailable_providers: [
          { provider: "claude", reason: "not_authenticated", remediation: "Sign in to claude on this machine" },
          { provider: "codex", reason: "not_authenticated", remediation: "Sign in to codex on this machine" },
        ],
      },
      provider_readiness: {
        claude: { state: "not_authenticated", remediation: "Sign in to claude on this machine" },
        codex: { state: "not_authenticated", remediation: "Sign in to codex on this machine" },
        omp: { state: "unknown" },
      },
    },
  ],
};

// A launchable machine so the launch sheet renders its happy path, including
// the Model row: the coding agent is chosen here, and the model is either the
// provider's own default or one of the ids this machine last ran for it.
// Recency is the whole point of that row, so the timestamps are derived from
// the clock the capture freezes the page at (see fixtureNowIso) rather than
// from real time -- real "now" sits months in the future of that clock, which
// rendered every row as an absolute date instead of "2 hours ago".
const LAUNCH_FIXTURE_NOW = Date.parse("2026-04-15T16:12:00Z");
const hoursBeforeFixtureNow = (hours: number): string => new Date(LAUNCH_FIXTURE_NOW - hours * 3_600_000).toISOString();

const LAUNCH_MODEL_PICKER_MACHINES = {
  machines: [
    {
      device_id: "workbench",
      machine_name: "workbench",
      online: true,
      control_channel_status: "connected",
      supports: ["codex.turn_start", "claude.turn_start"],
      control_operations_by_provider: { codex: ["turn_start"], claude: ["turn_start"] },
      last_seen_at: "2026-04-15T16:11:00Z",
      connected_since: "2026-04-15T12:00:00Z",
      engine_build: "fixture",
      launch: {
        blocked_by: null,
        providers: [{ provider: "codex" }, { provider: "claude" }],
        default_provider: "codex",
        unavailable_providers: [],
      },
      provider_readiness: {
        codex: { state: "ready" },
        claude: { state: "ready" },
      },
    },
  ],
};

/**
 * Landing scenes publish images, so no request may reach a real server: the
 * dev proxy would forward it to the operator's own instance and leak real
 * account data (initials, admin tabs) into marketing assets. Unmocked API
 * calls get an empty 404 and are logged so the fixture can cover them.
 */
const LANDING_APP_SHELL: Record<string, unknown> = {
  // A generic, non-admin viewer so no operator identity or admin tab shows.
  "/api/auth/status": {
    authenticated: true,
    user: {
      id: 1,
      email: "sam@example.com",
      display_name: "Sam Rivera",
      avatar_url: null,
      is_active: true,
      created_at: "2026-03-01T12:00:00Z",
      role: "USER",
    },
  },
  "/api/auth/methods": { google: false, password: false, sso: false, sso_url: null },
  "/api/health": { status: "healthy" },
};

async function sealOrFallback(route: Route, scene: SceneName, pathname: string): Promise<void> {
  if (LANDING_SCENES.includes(scene)) {
    if (pathname in LANDING_APP_SHELL) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(LANDING_APP_SHELL[pathname]) });
      return;
    }
    console.log(`  [landing] unmocked ${route.request().method()} ${pathname} -> 404`);
    await route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
    return;
  }
  await route.fallback();
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

  const fixtureNowIso = SESSION_DETAIL_SCENES.includes(scene)
    ? SESSION_DETAIL_STRESS_NOW
    : "2026-04-15T16:12:00Z";
  await page.addInitScript((nowIso) => {
    const fixtureNow = Date.parse(nowIso);
    Date.now = () => fixtureNow;
  }, fixtureNowIso);

  if (scene === "timeline-card-stress" || scene === "launch-unavailable" || LANDING_TIMELINE_SCENES.includes(scene)) {
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
  frameName: string = pageName,
  probe: string[] = [],
): Promise<CaptureResult> {
  const query = scene === "landing-search" ? `?query=${encodeURIComponent(LANDING_SEARCH_QUERY)}` : "";
  const url = `${baseUrl}${PAGE_DEFINITIONS[pageName].path}${query}`;
  console.log(`  Navigating to ${url}...`);

  await installScenePageOverrides(page, scene, pageName);
  await page.goto(url);

  // Wait for page stability - prefer shared readiness flags.
  try {
    await page.waitForSelector("[data-screenshot-ready='true'], [data-ready='true']", { timeout: 5000 });
  } catch {
    await page.waitForLoadState("networkidle", { timeout: 10000 });
  }

  if (scene === "launch-unavailable") {
    await page.click("[data-testid='sessions-start-session']");
    await page.waitForSelector("[data-testid='launch-unavailable-providers']", { timeout: 5000 });
    await page.click("[data-testid='launch-signin-codex']");
    await page.waitForSelector("[data-testid='launch-signin-panel-codex']", { timeout: 5000 });
  }

  // Open the launch sheet and expand its Model row so the frame carries the
  // recents the launch picker offers on this machine.
  if (scene === "launch-model-picker") {
    await page.click("[data-testid='sessions-start-session']");
    await page.waitForSelector("[data-testid='launch-model-select']", { timeout: 5000 });
    await page.click("[data-testid='launch-model-select'] summary");
    await page.waitForSelector("[data-testid='launch-model-select-input']", { timeout: 5000 });
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

  if (scene === "session-resume" && pageName === "session-detail") {
    await page.getByRole("button", { name: /Resume on/ }).click();
    await page.getByRole("dialog").waitFor();
  }

  // Capture screenshot
  const screenshotPath = path.join(outputDir, `${frameName}.png`);
  await page.screenshot({ path: screenshotPath, fullPage: false });
  console.log(`  Screenshot: ${screenshotPath}`);

  if (probe.length > 0) {
    // Evaluated from a string so esbuild's keepNames helper is not injected.
    const script = `((selectors) => selectors.map((sel) => {
      const el = document.querySelector(sel);
      if (!el) return { selector: sel, found: false };
      const r = el.getBoundingClientRect();
      const c = getComputedStyle(el);
      const pick = ["display", "position", "flex", "flexGrow", "flexShrink", "flexBasis", "width", "maxWidth", "minWidth", "margin", "padding", "justifyContent", "alignItems", "alignSelf", "gap", "gridTemplateColumns"];
      const styles = {};
      for (const k of pick) styles[k] = c[k];
      return { selector: sel, found: true, box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }, className: String(el.className).slice(0, 120), styles };
    }))(${JSON.stringify(probe)})`;
    const probed = await page.evaluate(script);
    const probePath = path.join(outputDir, `${frameName}-probe.json`);
    writeFileSync(probePath, JSON.stringify({ viewport: page.viewportSize(), elements: probed }, null, 2));
    console.log(`  Probe: ${probePath}`);
  }

  // Capture accessibility snapshot
  let a11yPath: string | undefined;
  let a11yFormat: A11yFormat = "none";
  try {
    const accessibilitySnapshot =
      (page as unknown as { accessibility?: { snapshot?: () => Promise<unknown> } }).accessibility
        ?.snapshot;
    if (typeof accessibilitySnapshot === "function") {
      const a11yTree = await accessibilitySnapshot();
      a11yPath = path.join(outputDir, `${frameName}-a11y.json`);
      writeFileSync(a11yPath, JSON.stringify(a11yTree, null, 2));
      a11yFormat = "json";
    } else {
      const ariaSnapshot = await page.locator("body").ariaSnapshot();
      a11yPath = path.join(outputDir, `${frameName}-a11y.yml`);
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

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function isServing(url: string): Promise<boolean> {
  try {
    const response = await fetch(url);
    return response.status < 500;
  } catch {
    return false;
  }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Make sure something serves the web app at baseUrl. If nothing does and the
 * host is local, start Vite in web/ on that port and return a function that
 * stops it. If something already serves it, return a no-op: it is not ours.
 */
async function ensureFrontend(baseUrl: string): Promise<() => Promise<void>> {
  if (await isServing(baseUrl)) {
    console.log(`Frontend already serving at ${baseUrl} (not owned by this capture)`);
    return async () => {};
  }
  const target = new URL(baseUrl);
  if (!["localhost", "127.0.0.1", "[::1]"].includes(target.hostname)) {
    throw new Error(`Nothing serves ${baseUrl} and it is not local; start it or change FRONTEND_URL.`);
  }
  const port = target.port || "80";
  console.log(`Nothing listening at ${baseUrl}; starting Vite on :${port} for this capture...`);

  const output: string[] = [];
  const child = spawn("bunx", ["vite", "--port", port, "--strictPort", "--clearScreen", "false"], {
    cwd: path.join(REPO_ROOT, "web"),
    stdio: ["ignore", "pipe", "pipe"],
    detached: true,
  });
  child.stdout?.on("data", (chunk) => output.push(String(chunk)));
  child.stderr?.on("data", (chunk) => output.push(String(chunk)));

  const stop = async () => {
    if (child.exitCode !== null || child.signalCode !== null || child.pid == null) return;
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {
      /* already gone */
    }
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline && child.exitCode === null && child.signalCode === null) {
      await sleep(100);
    }
    if (child.exitCode === null && child.signalCode === null) {
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch {
        /* already gone */
      }
    }
    console.log("Stopped the Vite server this capture started.");
  };

  const onSignal = () => {
    void stop().finally(() => process.exit(130));
  };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);

  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Vite exited before serving:\n${output.join("")}`);
    }
    if (await isServing(baseUrl)) {
      console.log(`Vite ready at ${baseUrl}`);
      return stop;
    }
    await sleep(250);
  }
  await stop();
  throw new Error(`Vite did not start serving ${baseUrl} within 60s:\n${output.join("")}`);
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

  // Fixture-backed scenes answer every API call from Playwright routes, so
  // they need only the Vite server; the backend gate is for demo-data scenes.
  if (sceneUsesMockApi(opts.scene)) {
    console.log(`Fixture scene "${opts.scene}": API answered by Playwright routes, no backend needed`);
  } else if (await checkDevRunning(opts.backendUrl)) {
    console.log(`Backend healthy at ${opts.backendUrl}`);
  } else {
    console.error(`Scene "${opts.scene}" needs the backend, and nothing answers at ${opts.backendUrl}/api/health`);
    console.error("Start it with: make dev-demo   (or make dev, then re-run)");
    process.exit(1);
  }

  const stopFrontend = await ensureFrontend(opts.baseUrl);

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
    // A tone scene renders the same page once per tone into its own frame.
    const frames: Array<{ pageName: PageName; frameName: string; tone: SessionTone }> =
      opts.scene === "session-tones"
        ? SESSION_TONES.map((tone) => ({ pageName: "session-detail" as const, frameName: `session-detail-${tone}`, tone }))
        : pagesToCapture.map((pageName) => ({ pageName, frameName: pageName, tone: "running" as const }));
    for (const { pageName, frameName, tone } of frames) {
      console.log(`\n${frameName}:`);
      try {
        if (opts.scene === "session-tones") {
          await installSceneMocks(context, opts.scene, opts.baseUrl, tone);
        }
        artifacts[frameName] = await captureBundle(
          context,
          page,
          pageName,
          outputDir,
          opts.baseUrl,
          opts.scene,
          frameName,
          opts.probe,
        );
      } catch (error) {
        const { message, detail } = formatError(error);
        errors.push(`[${frameName}] ${message}`);
        consoleLogs.push(`[CAPTURE_ERROR] ${detail}`);
        artifacts[frameName] = { a11yFormat: "none", error: message };
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
    await stopFrontend();

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
