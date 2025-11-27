// Runtime configuration for frontend deployment
// This file is loaded before the app and sets window.API_BASE_URL / window.WS_BASE_URL

// Local dev: use 127.0.0.1 to bypass system proxies that intercept "localhost" on port 80
// Production/other: use same-origin relative paths
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1"]);

if (LOCAL_HOSTS.has(window.location.hostname)) {
  const port = window.location.port || "80";
  window.API_BASE_URL = "http://127.0.0.1:" + port + "/api";
  window.WS_BASE_URL = "ws://127.0.0.1:" + port;
} else if (window.location.hostname === 'swarmlet.com') {
  window.API_BASE_URL = "https://api.swarmlet.com/api";
  window.WS_BASE_URL = "wss://api.swarmlet.com";
} else {
  window.API_BASE_URL = "/api";
  window.WS_BASE_URL = window.location.origin.replace("http", "ws");
}

console.log("Loaded runtime config:", {
  API_BASE_URL: window.API_BASE_URL,
  WS_BASE_URL: window.WS_BASE_URL,
  origin: window.location.origin
});
