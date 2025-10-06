// Runtime configuration for production deployment
// This file is loaded before the React app and sets global config variables

// API configuration - use relative paths for same-origin requests
window.API_BASE_URL = "/api";

// WebSocket configuration - relative path for same-origin WebSocket
// The nginx proxy will handle the WebSocket upgrade
window.WS_BASE_URL = window.location.origin.replace("http", "ws");

console.log("Loaded runtime config:", {
  API_BASE_URL: window.API_BASE_URL,
  WS_BASE_URL: window.WS_BASE_URL
});
