import { execSync } from "node:child_process";
import path from "node:path";
import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

/** Replace __BUILD_HASH__ in index.html with the short git SHA. */
function buildHashPlugin(): Plugin {
  let hash = "dev";
  return {
    name: "build-hash",
    configResolved() {
      try {
        hash = execSync("git rev-parse --short HEAD", { encoding: "utf8" }).trim();
      } catch {
        hash = Date.now().toString(36);
      }
    },
    transformIndexHtml(html) {
      return html.replace(/__BUILD_HASH__/g, hash);
    },
  };
}

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
  const proxyTarget = process.env.VITE_PROXY_TARGET || rootEnv.VITE_PROXY_TARGET || "http://backend:8000";

  return {
    plugins: [react(), buildHashPlugin()],
    base: basePath,
    resolve: {
      preserveSymlinks: false,
      // Prevent React duplication across workspaces/hoisting
      dedupe: ["react", "react-dom", "react/jsx-runtime", "react/jsx-dev-runtime"],
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
        "/config.js": {
          target: proxyTarget,
          changeOrigin: true,
        },
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
