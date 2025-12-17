import path from "node:path";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // Load .env from repo root (monorepo root contains single .env file)
  const repoRoot = path.resolve(__dirname, "../../..");
  const rootEnv = loadEnv(mode, repoRoot, "");

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
    resolve: {
      // Ensure Vite resolves dependencies from workspace root node_modules
      preserveSymlinks: false,
      // Force React to resolve from local node_modules to prevent version mismatches in tests
      alias: {
        react: path.resolve(__dirname, "node_modules/react"),
        "react-dom": path.resolve(__dirname, "node_modules/react-dom"),
        "react/jsx-runtime": path.resolve(__dirname, "node_modules/react/jsx-runtime"),
        "react/jsx-dev-runtime": path.resolve(__dirname, "node_modules/react/jsx-dev-runtime"),
      },
    },
    server: {
      host: "0.0.0.0",
      port: frontendPort,
      // Enable file watching with polling for Docker volumes
      watch: {
        usePolling: true,
        interval: 1000,
      },
      proxy: {
        "/api/ws": {
          target: proxyTarget,
          ws: true,
          changeOrigin: true,
        },
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
