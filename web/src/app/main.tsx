import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient } from "@tanstack/react-query";
import { BrowserRouter } from "react-router";
import config from "@/shared/lib/config";
import { shouldRetryQuery } from "./queryRetry";
import { loadRouteChunks } from "./routeChunks";

// Global stylesheet entrypoint
import "./styles/app.css";
import { AppContent, AppProviders } from "./AppRoot";

// Umami analytics is driven by runtime config.js.
// Vite env fallback remains only for older standalone frontend deployments.
const isLocalhost = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";
const umamiWebsiteId = config.umamiWebsiteId;
const umamiScriptSrc = config.umamiScriptSrc;
const umamiDomains = config.umamiDomains;
const umamiTag = config.umamiTag;

// Both halves are required: a website id with no script URL would inject
// <script src="">, which re-requests the page itself instead of a tracker.
if (!isLocalhost && umamiWebsiteId && umamiScriptSrc) {
  const script = document.createElement("script");
  script.defer = true;
  script.src = umamiScriptSrc;
  script.dataset.websiteId = umamiWebsiteId;
  if (umamiDomains) {
    script.dataset.domains = umamiDomains;
  }
  if (umamiTag) {
    script.dataset.tag = umamiTag;
  }
  script.dataset.performance = "true";
  document.head.appendChild(script);

  const recorder = document.createElement("script");
  recorder.defer = true;
  recorder.src = umamiScriptSrc.replace("script.js", "recorder.js");
  recorder.dataset.websiteId = umamiWebsiteId;
  recorder.dataset.sampleRate = "1";
  recorder.dataset.maskLevel = "moderate";
  recorder.dataset.maxDuration = "1800000";
  document.head.appendChild(recorder);
}

// Global error beacon - captures JS errors from all users (including anonymous)
if (!config.demoMode) {
  window.onerror = (msg, src, line, col, err) => {
    fetch("/api/ops/beacon", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ msg, src, line, col, stack: err?.stack, url: location.href }),
      keepalive: true,
    }).catch(() => {}); // Silent fail
  };

  window.onunhandledrejection = (event) => {
    fetch("/api/ops/beacon", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        msg: event.reason?.message || String(event.reason),
        stack: event.reason?.stack,
        url: location.href,
        type: "unhandled_rejection",
      }),
      keepalive: true,
    }).catch(() => {});
  };
}

const container = document.getElementById("react-root");

if (!container) {
  throw new Error("React root container not found");
}

function parseUiEffects(value: string | null): "on" | "off" | null {
  if (!value) return null;
  const normalized = value.trim().toLowerCase();
  if (normalized === "on" || normalized === "1" || normalized === "true" || normalized === "yes") return "on";
  if (normalized === "off" || normalized === "0" || normalized === "false" || normalized === "no") return "off";
  return null;
}

// UI Effects toggle - defaults to "on". Disable via:
// - VITE_UI_EFFECTS=off
// - ?uieffects=off or ?effects=off
const envUiEffects = parseUiEffects(import.meta.env.VITE_UI_EFFECTS);
const params = new URLSearchParams(window.location.search);
const queryUiEffects = parseUiEffects(params.get("uieffects") ?? params.get("effects"));
// Default: "on" (full visual mode). Use env/query to force "off".
const uiEffects: "on" | "off" = queryUiEffects ?? envUiEffects ?? "on";
// A prerendered page already carries its own setting (the blog turns effects
// off); only an explicit override replaces it.
if (!container.hasChildNodes() || queryUiEffects !== null || envUiEffects !== null) {
  container.setAttribute("data-ui-effects", uiEffects);
}

// Deterministic mode flags for video recording
// ?clock=frozen - freeze time display (Apple-style 9:41 AM)
const clockFrozen = params.get("clock") === "frozen";
if (clockFrozen) {
  const frozenTime = new Date("2026-01-15T09:41:00");
  const frozenTimestamp = frozenTime.getTime();

  // Expose frozen time globally for components to use
  (window as Window & { __FROZEN_TIME?: Date }).__FROZEN_TIME = frozenTime;
  document.body.classList.add("clock-frozen");

  // Monkey-patch Date.now() to return frozen time
  // This is simpler and covers most use cases (timestamps, relative time)
  const OriginalDateNow = Date.now;
  Date.now = () => frozenTimestamp;

  // Store original for potential restoration
  (window as Window & { __ORIGINAL_DATE_NOW?: typeof Date.now }).__ORIGINAL_DATE_NOW = OriginalDateNow;
}

// ?seed=X - seed random values for consistent layout (deterministic)
const randomSeed = params.get("seed");
if (randomSeed) {
  // Simple seeded PRNG (mulberry32)
  const seed = randomSeed.split("").reduce((a, c) => ((a << 5) - a + c.charCodeAt(0)) | 0, 0);
  let t = seed >>> 0;
  const seededRandom = () => {
    t = (t + 0x6d2b79f5) | 0;
    let r = Math.imul(t ^ (t >>> 15), t | 1);
    r ^= r + Math.imul(r ^ (r >>> 7), r | 61);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
  Math.random = seededRandom;
  (window as Window & { __RANDOM_SEED?: string }).__RANDOM_SEED = randomSeed;
}

// ?replay=X - replay scenario name (passed to backend for deterministic responses)
const replayScenario = params.get("replay");
if (replayScenario) {
  (window as Window & { __REPLAY_SCENARIO?: string }).__REPLAY_SCENARIO = replayScenario;
  document.body.classList.add("replay-mode");
}

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: shouldRetryQuery } },
});

const app = (
  <React.StrictMode>
    <AppProviders queryClient={queryClient}>
      <BrowserRouter>
        <AppContent />
      </BrowserRouter>
    </AppProviders>
  </React.StrictMode>
);

// Marketing routes ship as prerendered HTML (web/scripts/prerender.mjs): adopt
// that DOM instead of rebuilding it. Everywhere else the root is empty.
if (container.hasChildNodes()) {
  const hydrate = () =>
    ReactDOM.hydrateRoot(container, app, {
      // A hydration mismatch is a prerender bug that tests/prerender catches, not
      // a user-facing fault: keep it out of window.onerror, which beacons every
      // uncaught error to /api/ops/beacon.
      onRecoverableError: (error) => console.warn("[hydrate]", error),
    });
  // A lazy public route (the docs) has its chunk modulepreloaded by the page's
  // <head>: have it in hand before hydrating so React adopts the static page
  // without waiting. A failed load hydrates anyway; the lazy route handles it.
  void loadRouteChunks(window.location.pathname).then(hydrate, hydrate);
} else {
  ReactDOM.createRoot(container).render(app);
}
