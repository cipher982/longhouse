import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { Toaster } from "react-hot-toast";
import { AuthProvider } from "./lib/auth";
import { ConfirmProvider } from "./components/confirm";

// CSS Layer order declaration (MUST be first)
import "./styles/layers.css";

// Core styles (tokens defined in legacy.css via styles/tokens.css)
import "./styles/legacy.css";

// Shared UI primitives (@layer components)
import "./styles/ui.css";

// Component styles (@layer components)
import "./styles/modal.css";
import "./styles/chat.css";

// Page styles (@layer pages)
import "./styles/profile-admin.css";
import "./styles/settings.css";
import "./styles/css/agent-settings.css";
import "./styles/marketing-mode.css";
import "./styles/reliability.css";
import "./styles/trace-explorer.css";
import App from "./routes/App";

// Umami Analytics - env-configurable via Vite (VITE_UMAMI_*)
// Only loads on production domains (not localhost)
const isLocalhost = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";
const umamiWebsiteId = import.meta.env.VITE_UMAMI_WEBSITE_ID;
const umamiScriptSrc = import.meta.env.VITE_UMAMI_SCRIPT_SRC || "https://analytics.drose.io/script.js";
const umamiDomains = import.meta.env.VITE_UMAMI_DOMAINS;

if (!isLocalhost && umamiWebsiteId) {
  const script = document.createElement("script");
  script.defer = true;
  script.src = umamiScriptSrc;
  script.dataset.websiteId = umamiWebsiteId;
  if (umamiDomains) {
    script.dataset.domains = umamiDomains;
  }
  document.head.appendChild(script);
}

// Global error beacon - captures JS errors from all users (including anonymous)
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
container.setAttribute("data-ui-effects", uiEffects);

// Marketing mode toggle - enables vivid styling for screenshots
// Activated via ?marketing=true
const isMarketingMode = params.get("marketing") === "true";
if (isMarketingMode) {
  document.body.classList.add("marketing-mode");
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

const queryClient = new QueryClient();

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ConfirmProvider>
          <BrowserRouter
            future={{
              v7_startTransition: true,
              v7_relativeSplatPath: true,
            }}
          >
            <App />
            <Toaster
            position="top-right"
            toastOptions={{
              duration: 4000,
              style: {
                background: '#27272a',
                color: '#fafafa',
                border: '1px solid #3f3f46',
                borderRadius: '8px',
                fontSize: '14px',
                fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
              },
              success: {
                duration: 3000,
                iconTheme: {
                  primary: '#10b981',
                  secondary: '#fafafa',
                },
              },
              error: {
                duration: 6000,
                iconTheme: {
                  primary: '#ef4444',
                  secondary: '#fafafa',
                },
              },
            }}
          />
          </BrowserRouter>
        </ConfirmProvider>
      </AuthProvider>
    </QueryClientProvider>
  </React.StrictMode>
);
