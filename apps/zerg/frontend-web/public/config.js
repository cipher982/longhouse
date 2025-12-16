// Runtime configuration for frontend deployment
// This file is loaded before the app and sets window.API_BASE_URL / window.WS_BASE_URL

// Local dev: use 127.0.0.1 to bypass system proxies that intercept "localhost" on port 80
// Production/other: use same-origin relative paths
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1"]);

if (LOCAL_HOSTS.has(window.location.hostname)) {
  const port = window.location.port || "80";
  window.API_BASE_URL = "http://127.0.0.1:" + port + "/api";
  window.WS_BASE_URL = "ws://127.0.0.1:" + port;
} else {
  // Production (swarmlet.com) and other environments: same-origin paths
  // This enables HttpOnly cookie auth without cross-origin complexity
  window.API_BASE_URL = "/api";
  window.WS_BASE_URL = window.location.origin.replace("http", "ws");
}

console.log("Loaded runtime config:", {
  API_BASE_URL: window.API_BASE_URL,
  WS_BASE_URL: window.WS_BASE_URL,
  origin: window.location.origin
});

// Umami Analytics - only on production domain
if (!LOCAL_HOSTS.has(window.location.hostname)) {
  const script = document.createElement('script');
  script.defer = true;
  script.src = 'https://analytics.drose.io/script.js';
  script.dataset.websiteId = '486eaa80-2916-41ee-a2a2-f55209495028';
  script.dataset.domains = 'swarmlet.com';
  document.head.appendChild(script);
  console.log("Umami analytics loaded for production domain");
} else {
  console.log("Umami analytics skipped (localhost)");
}
