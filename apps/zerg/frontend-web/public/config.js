// Runtime configuration for frontend deployment
// This file is a template rendered by nginx at container startup
// Environment variables are injected here during the build/startup process

// API configuration: Always use relative paths for same-origin requests
// Whether in Docker or behind a proxy, the frontend is always served from the same origin as /api
window.API_BASE_URL = "/api";

// WebSocket configuration: Replace http/https with ws/wss, keeping the same origin
window.WS_BASE_URL = window.location.origin.replace("http", "ws");

console.log("Loaded runtime config:", {
  API_BASE_URL: window.API_BASE_URL,
  WS_BASE_URL: window.WS_BASE_URL,
  origin: window.location.origin
});
