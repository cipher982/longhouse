import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { Toaster } from "react-hot-toast";
import { AuthProvider } from "./lib/auth";
import "./styles/legacy.css";
import "./styles/chat.css";
import "./styles/profile-admin.css";
import "./styles/css/agent-settings.css";
import App from "./routes/App";

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
                background: '#363636',
                color: '#fff',
              },
              success: {
                duration: 3000,
              },
              error: {
                duration: 6000,
              },
            }}
          />
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  </React.StrictMode>
);
