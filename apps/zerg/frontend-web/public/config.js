// Runtime configuration for frontend deployment
// This file is a template rendered by nginx at container startup
// Environment variables are injected here during the build/startup process

// API configuration: Always use relative paths for same-origin requests
// Whether in Docker or behind a proxy, the frontend is always served from the same origin as /api
window.API_BASE_URL = "/api";

// WebSocket configuration:
// - In production (swarmlet.com): Connect directly to api.swarmlet.com since Coolify
//   deploys frontend and backend as separate services (nginx can't proxy to "backend")
// - In development (localhost): Use same-origin WebSocket via nginx proxy
if (window.location.hostname === 'swarmlet.com') {
  // Production: Connect directly to the API server for WebSocket
  window.WS_BASE_URL = "wss://api.swarmlet.com";
} else if (window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1') {
  // Other production domains: Use same-origin WebSocket
  window.WS_BASE_URL = window.location.origin.replace("http", "ws");
}
// In dev (localhost), WS_BASE_URL stays undefined and falls back to VITE_WS_BASE_URL

console.log("Loaded runtime config:", {
  API_BASE_URL: window.API_BASE_URL,
  WS_BASE_URL: window.WS_BASE_URL || '(will use VITE_WS_BASE_URL)',
  origin: window.location.origin
});
