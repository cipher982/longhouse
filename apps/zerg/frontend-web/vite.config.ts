import path from "node:path";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // Load .env from frontend-web directory (where .env.example lives)
  const rootEnv = loadEnv(mode, __dirname, "");

  const frontendPort = Number(rootEnv.FRONTEND_PORT || 3000);

  // Use root path for both dev and production
  // The /react/ path was legacy and unnecessary - only frontend runs on this port
  const basePath = "/";

  // Proxy target: use VITE_PROXY_TARGET for local dev outside Docker,
  // otherwise leverage Docker Compose DNS (backend:8000)
  const proxyTarget = rootEnv.VITE_PROXY_TARGET || "http://backend:8000";

  return {
    plugins: [react()],
    base: basePath,
    server: {
      host: "127.0.0.1",
      port: frontendPort,
      // Enable file watching with polling for Docker volumes
      watch: {
        usePolling: true,
        interval: 1000,
      },
      proxy: {
        "/api": {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
    build: {
      sourcemap: true,
      outDir: "dist",
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
    },
  };
});
