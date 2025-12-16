import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { Toaster } from "react-hot-toast";
import { AuthProvider } from "./lib/auth";
import "./styles/legacy.css";
import "./styles/chat.css";
import "./styles/profile-admin.css";
import "./styles/settings.css";
import "./styles/css/agent-settings.css";
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

const queryClient = new QueryClient();

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
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
      </AuthProvider>
    </QueryClientProvider>
  </React.StrictMode>
);
