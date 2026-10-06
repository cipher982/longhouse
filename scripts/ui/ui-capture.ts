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
 *   bunx tsx scripts/ui/ui-capture.ts [page] [--scene=X] [--viewport=X] [--output=X] [--all] [--no-trace] [--probe=sel1,sel2] [--wheel-map] [--css-variant=X] [--action=step;step]
 *
 * --action runs steps after the page settles and before the screenshot, so a
 * frame can show an opened popover or dialog: `click:<selector>` or
 * `press:<key>` (Playwright key names, e.g. `press:Meta+k`), separated by ";".
 *
 * --css-variant injects scripts/ui/css-variants/<X>.css after the page loads:
 * a layout experiment to look at, never shipped CSS (e.g. terminal density).
 *
 * --wheel-map sends a real wheel event at every 40px cell and writes
 * <page>-wheelmap.txt/.json: which scroll container moved (or "." = nothing),
 * so gutters and rails that fail to scroll the transcript show up as dead cells.
 *
 * --probe writes <page>-probe.json with the bounding box and key computed
 * styles of each selector (first match), so a layout can be measured, not
 * just eyeballed.
 *
 * Examples:
 *   bunx tsx scripts/ui/ui-capture.ts timeline
 *   bunx tsx scripts/ui/ui-capture.ts --scene=empty
 *   bunx tsx scripts/ui/ui-capture.ts timeline --scene=timeline-card-stress --viewport=mobile
 *   bunx tsx scripts/ui/ui-capture.ts session-detail --scene=session-detail-stress
 *   bunx tsx scripts/ui/ui-capture.ts session-detail --scene=session-prose-idle --viewport=2000x1200 --css-variant=terminal
 *   bunx tsx scripts/ui/ui-capture.ts session-detail --scene=session-input-outbox --viewport=mobile
 *   bunx tsx scripts/ui/ui-capture.ts session-detail --scene=session-remote-image-outbox --viewport=mobile
 *   bunx tsx scripts/ui/ui-capture.ts session-detail --scene=session-wake-origin
 *   bunx tsx scripts/ui/ui-capture.ts session-detail --scene=session-resume
 *   bunx tsx scripts/ui/ui-capture.ts session-detail --scene=session-ended   # the ended-run notice, resume and branch, no modal
 *   bunx tsx scripts/ui/ui-capture.ts session-detail --scene=session-tones   # one PNG per composer tone
 *   bunx tsx scripts/ui/ui-capture.ts session-detail --scene=session-background-notices   # collapsed and expanded PNGs
 *   bunx tsx scripts/ui/ui-capture.ts devices --scene=devices-revoke
 *   bunx tsx scripts/ui/ui-capture.ts landing --scene=provider-certification --viewport=desktop-tall
 *   bunx tsx scripts/ui/ui-capture.ts machines
 *   bunx tsx scripts/ui/ui-capture.ts --all
 */

import { chromium, type BrowserContext, type Page, type Route } from "playwright";
import { execSync } from "child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "fs";
import path from "path";
import { pathToFileURL } from "url";
import { ensureFrontend, REPO_ROOT } from "./frontend";
import {
  buildSessionBackgroundNoticesFixture,
  buildSessionDetailStressFixture,
  buildSessionProseIdleFixture,
  buildRailSessionsFixture,
  buildSessionQuestionFixture,
  buildSessionAttentionFixture,
  buildSessionResumeFixture,
  buildSessionStaleObservationFixture,
  buildSessionToneFixture,
  SESSION_DETAIL_STRESS_NOW,
  SESSION_DETAIL_STRESS_SESSION_ID,
  SESSION_WAKE_ORIGIN_INPUT_RECEIPTS,
  SESSION_TONES,
  type SessionTone,
} from "../ui-fixtures/sessionDetailStress";
import { captureWheelMap } from "./wheel-map";
import { buildProviderCertificationFixture } from "../ui-fixtures/providerCertification";
import { buildFirstRunMachineFixture, FIRST_RUN_NOW } from "../ui-fixtures/firstRun";
import { buildFirstRunMachinesSummary, buildMachinesFleetFixture } from "../ui-fixtures/machinesFleet";
import { buildTimelineCardStressFixture } from "../ui-fixtures/timelineCardStress";
import { buildTimelineHearthFixture, buildTimelineHearthStreamBatch } from "../ui-fixtures/timelineHearth";
import {
  LANDING_SEARCH_QUERY,
  buildLandingSessionFixture,
  buildLandingTimelineFixture,
} from "../ui-fixtures/landingShowcase";

export const PAGE_DEFINITIONS = {
  timeline: { path: "/timeline" },
  "session-detail": { path: `/timeline/${SESSION_DETAIL_STRESS_SESSION_ID}` },
  machines: { path: "/machines" },
  "machine-detail": { path: "/machines/cinder" },
  settings: { path: "/settings" },
  profile: { path: "/profile" },
  integrations: { path: "/settings/integrations" },
  devices: { path: "/settings/devices" },
  // The public marketing page (always reachable, even when authenticated).
  landing: { path: "/landing" },
  // Public legal pages: static copy. Capture them with SCENE=first-run so the
  // app shell's API calls are answered by fixtures, not proxied to a real host.
  security: { path: "/security" },
  privacy: { path: "/privacy" },
  // The self-host password sign-in. Only the `login` scene can render it.
  login: { path: "/login" },
} as const;
type PageName = keyof typeof PAGE_DEFINITIONS;
const PAGES = Object.keys(PAGE_DEFINITIONS) as PageName[];
const PUBLIC_PAGES: readonly PageName[] = ["landing", "security", "privacy"];
const ALL_CAPTURE_PAGES = PAGES.filter(
  (pageName) => pageName !== "session-detail" && pageName !== "login" && !PUBLIC_PAGES.includes(pageName),
);

const SCENES = [
  "empty",
  "demo",
  "onboarding-modal",
  "missing-api-key",
  "timeline-card-stress",
  "timeline-error",
  "timeline-hearth",
  "launch-unavailable",
  "launch-no-machines",
  "launch-model-picker",
  "launch-model-picked",
  "session-detail-stress",
  "session-prose-idle",
  "session-input-outbox",
  "session-remote-image-outbox",
  "session-wake-origin",
  "session-question",
  "session-attention",
  "session-resume",
  "session-ended",
  "session-stale-observation",
  "session-tones",
  "session-background-notices",
  "landing",
  "landing-search",
  "landing-session",
  "provider-certification",
  "first-run",
  "first-run-machine",
  "login",
  "devices-revoke",
  "machines-fleet",
  "machines-unavailable",
] as const;
type SceneName = (typeof SCENES)[number];

/** Curated landing-page showcase data (scripts/ui-fixtures/landingShowcase.ts). */
const LANDING_TIMELINE_SCENES: readonly SceneName[] = ["landing", "landing-search"];
// Scenes that render the launch sheet with a machine that has run models.
const LAUNCH_MODEL_SCENES: readonly SceneName[] = ["launch-model-picker", "launch-model-picked"];
// The marketing page's provider chart, answered by the certification fixture.
const PROVIDER_CERTIFICATION_SCENE: SceneName = "provider-certification";
const LANDING_SCENES: readonly SceneName[] = [...LANDING_TIMELINE_SCENES, "landing-session", PROVIDER_CERTIFICATION_SCENE];
// A brand-new Runtime Host: no sessions, no machines. The timeline shows its
// connect command; the machines page opens its Connect a machine sheet.
const FIRST_RUN_SCENE: SceneName = "first-run";
// The same host a moment later: one Machine Agent online and five imported
// sessions with no live evidence. Timeline and Machines show it.
const FIRST_RUN_MACHINE_SCENE: SceneName = "first-run-machine";
// The self-host password sign-in, signed out, with password auth configured.
const LOGIN_SCENE: SceneName = "login";
// The Devices page with a machine holding two valid tokens (each `longhouse
// auth` mints one) and a revoked one, framed on the revoke-machine confirmation.
const DEVICES_REVOKE_SCENE: SceneName = "devices-revoke";
// A personal fleet (live Mac, signed-out bench box, idle box, a server with no
// live connection, quiet machines) for PAGE=machines and PAGE=machine-detail;
// machines-unavailable serves the same directory but fails the summary read.
const MACHINES_SCENES: readonly SceneName[] = ["machines-fleet", "machines-unavailable"];

const SESSION_DETAIL_SCENES: readonly SceneName[] = [
  "landing-session",
  "session-detail-stress",
  "session-prose-idle",
  "session-input-outbox",
  "session-remote-image-outbox",
  "session-wake-origin",
  "session-question",
  "session-attention",
  "session-resume",
  "session-ended",
  "session-stale-observation",
  "session-tones",
  "session-background-notices",
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
  // The whole timeline fixture in one frame, at 2x so small instruments
  // (status lamps, chips) can be inspected, not just located.
  "desktop-tall": {
    width: 1280,
    height: 1400,
    isMobile: false,
    hasTouch: false,
    deviceScaleFactor: 2,
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
  wheelMap: boolean;
  cssVariant: string | null;
  actions: string[];
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
  const wheelMap = args.includes("--wheel-map");
  const cssVariant = args.find((a) => a.startsWith("--css-variant="))?.slice("--css-variant=".length) || null;
  const actionArg = args.find((a) => a.startsWith("--action="))?.slice("--action=".length) ?? "";

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
    wheelMap,
    cssVariant,
    actions: actionArg.split(";").map((step) => step.trim()).filter(Boolean),
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
    scene === "timeline-error" ||
    scene === "timeline-hearth" ||
    scene === "launch-unavailable" ||
    scene === "launch-no-machines" ||
    scene === "launch-model-picker" ||
    scene === "launch-model-picked" ||
    LANDING_TIMELINE_SCENES.includes(scene) ||
    scene === "landing-session" ||
    scene === PROVIDER_CERTIFICATION_SCENE ||
    scene === "session-detail-stress" ||
    scene === "session-prose-idle" ||
    scene === "session-input-outbox" ||
    scene === "session-remote-image-outbox" ||
    scene === "session-wake-origin" ||
    scene === "session-question" ||
    scene === "session-attention" ||
    scene === "session-resume" ||
    scene === "session-ended" ||
    scene === "session-stale-observation" ||
    scene === "session-tones" ||
    scene === "session-background-notices" ||
    scene === FIRST_RUN_SCENE ||
    scene === FIRST_RUN_MACHINE_SCENE ||
    scene === LOGIN_SCENE ||
    scene === DEVICES_REVOKE_SCENE ||
    MACHINES_SCENES.includes(scene)
  );
}

const CSS_VARIANTS_DIR = path.join(REPO_ROOT, "scripts/ui/css-variants");

function cssVariantPath(name: string): string {
  return path.join(CSS_VARIANTS_DIR, `${name}.css`);
}

function validateOptions(opts: Options): void {
  if (opts.cssVariant && !existsSync(cssVariantPath(opts.cssVariant))) {
    throw new Error(`--css-variant=${opts.cssVariant}: no ${path.relative(REPO_ROOT, cssVariantPath(opts.cssVariant))}`);
  }
  if ((opts.scene === PROVIDER_CERTIFICATION_SCENE) !== (opts.page === "landing")) {
    throw new Error(`PAGE=landing and --scene=${PROVIDER_CERTIFICATION_SCENE} capture only each other.`);
  }
  if (opts.scene === DEVICES_REVOKE_SCENE && opts.page !== "devices") {
    throw new Error(`--scene=${DEVICES_REVOKE_SCENE} captures PAGE=devices only.`);
  }
  if ((opts.scene === LOGIN_SCENE) !== (opts.page === "login")) {
    throw new Error(`PAGE=login and --scene=${LOGIN_SCENE} capture only each other.`);
  }
  if (opts.scene === FIRST_RUN_MACHINE_SCENE && !["timeline", "machines", "machine-detail"].includes(opts.page)) {
    throw new Error(`--scene=${FIRST_RUN_MACHINE_SCENE} captures PAGE=timeline, PAGE=machines or PAGE=machine-detail.`);
  }
  if (MACHINES_SCENES.includes(opts.scene) && opts.page !== "machines" && opts.page !== "machine-detail") {
    throw new Error(`--scene=${opts.scene} captures PAGE=machines or PAGE=machine-detail.`);
  }
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

export async function installSceneMocks(
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
        : scene === "session-resume" || scene === "session-ended"
        ? buildSessionResumeFixture()
        : scene === "session-prose-idle"
          ? buildSessionProseIdleFixture()
        : scene === "session-question"
          ? buildSessionQuestionFixture()
          : scene === "session-attention"
          ? buildSessionAttentionFixture()
          : scene === "session-stale-observation"
          ? buildSessionStaleObservationFixture()
          : scene === "session-tones"
            ? buildSessionToneFixture(tone)
            : scene === "session-background-notices"
              ? buildSessionBackgroundNoticesFixture()
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

      if (pathname === `${sessionBasePath}/subagents`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ subagents: [] }),
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

      // The rail warms its neighbours' workspaces while idle; they have no
      // transcript in this scene.
      if (/^\/api\/timeline\/sessions\/rail-[^/]+\/workspace$/.test(pathname)) {
        await route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
        return;
      }

      // The session rail's list: the timeline's first page.
      if (pathname === "/api/timeline/sessions") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(buildRailSessionsFixture(fixture.session)),
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

      if (pathname === `/api/sessions/${fixture.session.id}/inputs` && (scene === "landing-session" || scene === "session-prose-idle")) {
        await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
        return;
      }

      if (pathname === `/api/sessions/${fixture.session.id}/inputs` && scene === "session-input-outbox") {
        await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
        return;
      }
      if (pathname === `/api/sessions/${fixture.session.id}/inputs` && scene === "session-remote-image-outbox") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: 9002,
              live_input_id: "remote-image-input",
              client_request_id: "remote-image-input",
              text: "",
              intent: "auto",
              status: "delivered",
              delivery_status: "delivered",
              attachments: [
                { filename: "reference.png", mime_type: "image/png", byte_size: 123 },
              ],
              turn: {
                turn_id: "remote-image-turn",
                receipt_id: "remote-image-input",
                run_id: "remote-image-run",
                state: "active",
                is_fresh: true,
              },
              created_at: "2026-04-15T16:10:45Z",
            },
            {
              id: 9003,
              live_input_id: "stale-console-input",
              client_request_id: "stale-console-input",
              text: "An older Console turn",
              intent: "auto",
              status: "delivered",
              delivery_status: "delivered",
              attachments: [],
              turn: {
                turn_id: "stale-console-turn",
                receipt_id: "stale-console-input",
                run_id: "stale-console-run",
                state: "active",
                is_fresh: false,
              },
              created_at: "2026-04-15T14:00:00Z",
            },
          ]),
        });
        return;
      }
      if (
        pathname === `/api/sessions/${fixture.session.id}/inputs` &&
        scene === "session-wake-origin"
      ) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(SESSION_WAKE_ORIGIN_INPUT_RECEIPTS),
        });
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

  if (MACHINES_SCENES.includes(scene)) {
    const fleet = buildMachinesFleetFixture();
    const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    await context.route(`${appOrigin}/api/**`, async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === "/api/timeline/machines") return route.fulfill(json(fleet.directory));
      if (pathname === "/api/timeline/machines/summary") {
        if (scene === "machines-unavailable") {
          return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: { code: "catalog_unavailable", message: "The session catalog is restarting." } }) });
        }
        return route.fulfill(json(fleet.summary));
      }
      if (pathname === "/api/runners/" || pathname === "/api/runners") return route.fulfill(json({ runners: fleet.runners }));
      await sealOrFallback(route, scene, pathname);
    });
    return;
  }

  if (scene === FIRST_RUN_MACHINE_SCENE || scene === LOGIN_SCENE) {
    const firstRun = buildFirstRunMachineFixture();
    const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    await context.route(`${appOrigin}/api/**`, async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (scene === LOGIN_SCENE) {
        // Signed out, password auth only: what a self-hosted Runtime Host serves
        // a browser with no session. A refused refresh means "no session", not "down".
        if (pathname === "/api/auth/status") return route.fulfill(json({ authenticated: false, user: null }));
        if (pathname === "/api/auth/methods") return route.fulfill(json({ google: false, password: true, sso: false, sso_url: null }));
        if (pathname === "/api/auth/refresh") return route.fulfill({ status: 401, contentType: "application/json", body: "{}" });
      } else {
        if (pathname === "/api/timeline/sessions") return route.fulfill(json(firstRun.sessions));
        if (pathname === "/api/timeline/filters") return route.fulfill(json(firstRun.filters));
        if (pathname === "/api/timeline/machines") return route.fulfill(json(firstRun.machines));
        if (pathname === "/api/timeline/machines/summary") return route.fulfill(json(buildFirstRunMachinesSummary(firstRun.machines)));
        if (pathname === "/api/runners/" || pathname === "/api/runners") return route.fulfill(json(firstRun.runners));
        if (pathname === "/api/timeline/sessions/stream") return route.fulfill({ status: 204, body: "" });
      }
      await sealOrFallback(route, scene, pathname);
    });
    return;
  }

  if (scene === PROVIDER_CERTIFICATION_SCENE) {
    const certification = buildProviderCertificationFixture();
    await context.route(`${appOrigin}/api/**`, async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === "/api/public/provider-certification") {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(certification) });
        return;
      }
      await sealOrFallback(route, scene, pathname);
    });
    return;
  }

  const fixture = scene === "timeline-hearth" ? buildTimelineHearthFixture() : buildTimelineCardStressFixture();
  // The hearth scene streams: each EventSource reconnect gets the next batch
  // of growing counts, so the fires see real tool/reply/prompt deltas.
  let hearthBatch = 0;

  await context.route(`${appOrigin}/api/**`, async (route) => {
    const requestUrl = new URL(route.request().url());
    const pathname = requestUrl.pathname;

    if (scene === FIRST_RUN_SCENE && pathname === "/api/timeline/sessions") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ...fixture.sessions, sessions: [], total: 0, has_real_sessions: false }),
      });
      return;
    }

    if (scene === DEVICES_REVOKE_SCENE && pathname === "/api/devices/tokens") {
      const token = (id: string, device_id: string, created_at: string, last_used_at: string | null, revoked_at: string | null = null) => ({
        id,
        device_id,
        created_at,
        last_used_at,
        revoked_at,
        is_valid: revoked_at === null,
      });
      const tokens = [
        token("11111111-1111-4111-8111-111111111111", "work-macbook", "2026-04-15T09:00:00Z", "2026-04-15T16:00:00Z"),
        token("22222222-2222-4222-8222-222222222222", "work-macbook", "2026-04-10T09:00:00Z", "2026-04-12T10:00:00Z"),
        token("33333333-3333-4333-8333-333333333333", "old-thinkpad", "2026-03-01T09:00:00Z", "2026-03-20T10:00:00Z", "2026-03-21T10:00:00Z"),
      ];
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ tokens, total: tokens.length }) });
      return;
    }

    if (scene === FIRST_RUN_SCENE && (pathname === "/api/runners/" || pathname === "/api/runners")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ runners: [] }) });
      return;
    }

    if (scene === FIRST_RUN_SCENE && pathname === "/api/timeline/machines") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ machines: [] }) });
      return;
    }

    if (scene === FIRST_RUN_SCENE && pathname === "/api/timeline/machines/summary") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ generated_at: new Date(FIRST_RUN_NOW).toISOString(), days: 14, utc_offset_minutes: 0, first_day: "2026-04-02", last_day: "2026-04-15", machines: [] }),
      });
      return;
    }

    // The page a one-session catalog overflow used to produce (2026-10-06).
    if (scene === "timeline-error" && pathname === "/api/timeline/sessions") {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          detail: {
            code: "shadow_fact_head_limit_exceeded",
            message: "Canonical session facts exceed the bounded timeline projection limit.",
          },
        }),
      });
      return;
    }

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

    if (pathname === "/api/timeline/sessions/stream" && scene === "timeline-hearth") {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
        body: buildTimelineHearthStreamBatch(hearthBatch++),
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

    if (scene === "launch-no-machines" && pathname === "/api/timeline/machines") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ machines: [] }) });
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

    if (LAUNCH_MODEL_SCENES.includes(scene) && pathname === "/api/timeline/machines") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(LAUNCH_MODEL_PICKER_MACHINES) });
      return;
    }

    if (LAUNCH_MODEL_SCENES.includes(scene) && pathname.startsWith("/api/timeline/machines/") && pathname.endsWith("/workspaces")) {
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

    if (LAUNCH_MODEL_SCENES.includes(scene) && pathname.includes("/providers/") && pathname.endsWith("/models")) {
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

/** Fixture API calls no scene answered. Printed at the end so a missing mock is loud. */
const unmockedCalls = new Set<string>();

async function sealOrFallback(route: Route, scene: SceneName, pathname: string): Promise<void> {
  // Fixture scenes are sealed: nothing reaches the dev proxy, which forwards
  // to a real Runtime Host with this machine's device token.
  if (pathname in LANDING_APP_SHELL) {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(LANDING_APP_SHELL[pathname]) });
    return;
  }
  // App chrome every fixture page polls; landing scenes keep their own (404) frames.
  if (!LANDING_SCENES.includes(scene) && scene !== FIRST_RUN_SCENE) {
    if (pathname === "/api/users/me/client-presence") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    // The nav's machine count reads the directory on every page.
    if (pathname === "/api/timeline/machines") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ machines: buildMachinesFleetFixture().directory.machines.slice(0, 2) }),
      });
      return;
    }
    const subagents = pathname.match(/^\/api\/timeline\/sessions\/([^/]+)\/subagents$/);
    if (subagents) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ session_id: subagents[1], children: [] }) });
      return;
    }
  }
  const call = `${route.request().method()} ${pathname}`;
  unmockedCalls.add(`[${scene}] ${call}`);
  if (LANDING_SCENES.includes(scene) || scene === FIRST_RUN_SCENE) {
    await route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
    return;
  }
  console.log(`  [${scene}] UNMOCKED ${call} -> 503 (fixture scenes never reach a real host)`);
  await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: `ui-capture: no fixture for ${call}` }) });
}

async function installScenePageOverrides(page: Page, scene: SceneName, pageName: PageName): Promise<void> {

  if (!sceneUsesMockApi(scene)) {
    return;
  }

  const fixtureNowIso = SESSION_DETAIL_SCENES.includes(scene)
    ? SESSION_DETAIL_STRESS_NOW
    : new Date(FIRST_RUN_NOW).toISOString();
  await page.addInitScript((nowIso) => {
    const fixtureNow = Date.parse(nowIso);
    Date.now = () => fixtureNow;
  }, fixtureNowIso);

  if (scene === "session-input-outbox") {
    await page.addInitScript((sessionId) => {
      const clientRequestId = "web-ui-retry-proof";
      window.localStorage.setItem(
        `longhouse:session-input:${sessionId}:${clientRequestId}`,
        JSON.stringify({
          sessionId,
          text: "Retry this image input",
          intent: "auto",
          clientRequestId,
          model: "gpt-5.6-luna",
          attachments: [],
          createdAt: Date.now(),
        }),
      );
      const sentRequestId = "web-ui-sent-summary-proof";
      window.localStorage.setItem(
        `longhouse:session-input:${sessionId}:${sentRequestId}`,
        JSON.stringify({
          sessionId,
          text: "Image delivered; waiting for its transcript echo.",
          intent: "auto",
          clientRequestId: sentRequestId,
          model: "gpt-5.6-luna",
          attachments: [
            { filename: "reference.png", type: "image/png", size: 123 },
          ],
          createdAt: Date.now(),
          deliveryConfirmed: true,
        }),
      );
    }, SESSION_DETAIL_STRESS_SESSION_ID);
  }

  if (scene === LOGIN_SCENE) {
    // Without a backend /config.js the dev server reads as auth-disabled and
    // /login redirects to the timeline; say this is a host that requires sign-in.
    await page.addInitScript(() => {
      (window as unknown as { __APP_MODE__?: string }).__APP_MODE__ = "production";
    });
  }

  if (
    scene === "timeline-card-stress" ||
    scene === "timeline-error" ||
    scene === "launch-unavailable" ||
    scene === "launch-no-machines" ||
    scene === FIRST_RUN_SCENE ||
    scene === FIRST_RUN_MACHINE_SCENE ||
    LANDING_TIMELINE_SCENES.includes(scene)
  ) {
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
  wheelMap = false,
  cssVariant: string | null = null,
  actions: string[] = [],
): Promise<CaptureResult> {
  const query = scene === "landing-search" ? `?query=${encodeURIComponent(LANDING_SEARCH_QUERY)}` : "";
  const url = `${baseUrl}${PAGE_DEFINITIONS[pageName].path}${query}`;
  console.log(`  Navigating to ${url}...`);

  await installScenePageOverrides(page, scene, pageName);
  // HEARTH_NO_WEBGL=1 renders the fires' static fallback glyphs.
  if (process.env.HEARTH_NO_WEBGL === "1") {
    await page.addInitScript(() => {
      Object.defineProperty(window, "WebGL2RenderingContext", { configurable: true, value: undefined });
    });
  }
  await page.goto(url);

  // `data-ready` allows interaction; an opted-in screenshot gate must also settle.
  let hasReadinessMarker = false;
  try {
    await page.waitForSelector("body[data-screenshot-ready], body[data-ready='true']", { timeout: 5000 });
    hasReadinessMarker = true;
  } catch {
    await page.waitForLoadState("networkidle", { timeout: 10000 });
  }
  if (hasReadinessMarker) {
    const screenshotGate = (await page.locator("body[data-screenshot-ready]").count()) > 0;
    await page.waitForSelector(
      screenshotGate ? "body[data-screenshot-ready='true']" : "body[data-ready='true']",
      { timeout: screenshotGate ? 12_000 : 5_000 }, // 12s is ~2.8x the observed 4.27s cold summary request.
    );
  }

  if (scene === "session-wake-origin") {
    const wakeRow = page.locator(
      '[data-testid="session-provider-notification"][data-origin="wake"]',
    );
    await wakeRow.waitFor({ state: "visible", timeout: 10_000 });
    await wakeRow.scrollIntoViewIfNeeded();
  }

  if (scene === DEVICES_REVOKE_SCENE) {
    await page.click("text=Revoke machine >> nth=0");
    await page.waitForSelector("[data-testid='confirm-dialog']", { timeout: 5000 });
  }

  if (scene === FIRST_RUN_SCENE && pageName === "machines") {
    await page.click("[data-testid='machines-connect-first-button']");
    await page.waitForSelector("[data-testid='connect-machine-command']", { timeout: 5000 });
  }

  // The chart fetches after first paint: wait for it to resolve, then frame it.
  if (scene === PROVIDER_CERTIFICATION_SCENE) {
    await page.waitForSelector("#providers [data-certification='certified']", { timeout: 5000 });
    await page.evaluate("document.getElementById('providers').scrollIntoView()");
  }

  // A host with sessions but no enrolled machine: the launch sheet explains
  // how to connect one.
  if (scene === "launch-no-machines") {
    await page.click("[data-testid='sessions-start-session']");
    await page.waitForSelector("[data-testid='launch-no-machines']", { timeout: 5000 });
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

  // Same sheet with a model actually chosen, so the collapsed row's caption
  // and value are both visible rather than assumed.
  if (scene === "launch-model-picked") {
    await page.click("[data-testid='sessions-start-session']");
    await page.waitForSelector("[data-testid='launch-model-select']", { timeout: 5000 });
    await page.click("[data-testid='launch-model-select'] summary");
    await page.getByRole("button", { name: /gpt-5\.6-luna/ }).click();
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

  if (cssVariant) {
    await page.addStyleTag({ content: readFileSync(cssVariantPath(cssVariant), "utf-8") });
  }

  // Let CSS apply
  await page.waitForTimeout(100);

  // Let the fires ignite, reach height, and take a few streamed tool batches.
  if (scene === "timeline-hearth") {
    await page.waitForTimeout(Number(process.env.HEARTH_WAIT_MS ?? 5000));
  }

  // The second frame of the notices scene: every expandable notice open but
  // the first, so the frame shows an open and a collapsed long one together.
  if (scene === "session-background-notices" && frameName.endsWith("-expanded")) {
    const heads = page.locator("[data-testid='session-provider-notification'] button");
    for (let index = (await heads.count()) - 1; index >= 1; index -= 1) {
      await heads.nth(index).click();
    }
    await page.waitForSelector("[data-testid='session-provider-notification-body']", { timeout: 5000 });
  }

  if (scene === "session-resume" && pageName === "session-detail") {
    await page.getByRole("button", { name: /Show resume command/ }).click();
    await page.getByRole("dialog").waitFor();
  }

  for (const step of actions) {
    const separator = step.indexOf(":");
    if (separator === -1) throw new Error(`--action step "${step}" needs a verb: click:<selector> or press:<key>`);
    const verb = step.slice(0, separator);
    const target = step.slice(separator + 1);
    if (verb === "click") await page.click(target);
    else if (verb === "press") await page.keyboard.press(target);
    else throw new Error(`Unknown --action step "${step}"; use click:<selector> or press:<key>`);
    await page.waitForTimeout(150);
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
      const pick = ["display", "position", "flex", "flexGrow", "flexShrink", "flexBasis", "width", "maxWidth", "minWidth", "margin", "padding", "justifyContent", "alignItems", "alignSelf", "gap", "gridTemplateColumns", "color", "backgroundColor", "borderColor"];
      const styles = {};
      for (const k of pick) styles[k] = c[k];
      return { selector: sel, found: true, box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }, className: String(el.className).slice(0, 120), styles };
    }))(${JSON.stringify(probe)})`;
    const probed = await page.evaluate(script);
    const probePath = path.join(outputDir, `${frameName}-probe.json`);
    writeFileSync(probePath, JSON.stringify({ viewport: page.viewportSize(), elements: probed }, null, 2));
    console.log(`  Probe: ${probePath}`);
  }

  if (wheelMap) {
    await captureWheelMap(page, outputDir, frameName);
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
    // SwiftShader gives headless Chromium WebGL2 with float render targets,
    // so the timeline's Hearth fires render instead of their static glyphs.
    browser = await chromium.launch({
      args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
    });
    context = await browser.newContext({
      viewport: { width: opts.viewport.width, height: opts.viewport.height },
      isMobile: opts.viewport.isMobile,
      hasTouch: opts.viewport.hasTouch,
      deviceScaleFactor: opts.viewport.deviceScaleFactor,
      // The hearth scene captures the fires moving (flames, sparks); every
      // other scene freezes motion, which also shows the fires' still frames.
      reducedMotion: opts.scene === "timeline-hearth" ? "no-preference" : "reduce",
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
        : opts.scene === "session-background-notices"
          ? ["collapsed", "expanded"].map((state) => ({
              pageName: "session-detail" as const,
              frameName: `session-detail-${state}`,
              tone: "running" as const,
            }))
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
          opts.wheelMap,
          opts.cssVariant,
          opts.actions,
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
        css_variant: opts.cssVariant,
      },
    };

    const manifestPath = path.join(outputDir, "manifest.json");
    writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));

    console.log(`\nManifest: ${manifestPath}`);
    if (unmockedCalls.size > 0) {
      console.warn(`\nUnmocked fixture API calls (answered 503/404, never proxied):\n  ${[...unmockedCalls].join("\n  ")}`);
    }
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

// Other probes import the scene mocks; only a direct run captures.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((e) => {
    console.error(e);
    process.exit(1);
  });
}
