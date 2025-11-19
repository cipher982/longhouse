// Runtime configuration for frontend deployment
// This file is a template rendered by nginx at container startup
// Environment variables are injected here during the build/startup process

// API configuration: Always use relative paths for same-origin requests
// Whether in Docker or behind a proxy, the frontend is always served from the same origin as /api
window.API_BASE_URL = "/api";

// WebSocket configuration: Replace http/https with ws/wss, keeping the same origin
// In development (localhost), let the app use VITE_WS_BASE_URL from docker-compose instead
// In production, use same-origin WebSocket connection
if (window.API_BASE_URL === "/api" || (window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1')) {
  window.WS_BASE_URL = window.location.origin.replace("http", "ws");
}

console.log("Loaded runtime config:", {
  API_BASE_URL: window.API_BASE_URL,
  WS_BASE_URL: window.WS_BASE_URL || '(will use VITE_WS_BASE_URL)',
  origin: window.location.origin
});
