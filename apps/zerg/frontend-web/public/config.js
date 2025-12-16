// Runtime configuration for frontend deployment
// This file is loaded before the app and sets window.API_BASE_URL / window.WS_BASE_URL

// Always use same-origin relative paths - this avoids CORS complexity
// and works correctly with nginx proxy in both dev and production.
// Previous logic that converted localhost to 127.0.0.1 caused cross-origin
// issues when credentials: 'include' was used (dev login, cookie auth).
window.API_BASE_URL = "/api";
window.WS_BASE_URL = window.location.origin.replace("http", "ws");

console.log("Loaded runtime config:", {
  API_BASE_URL: window.API_BASE_URL,
  WS_BASE_URL: window.WS_BASE_URL,
  origin: window.location.origin
});
