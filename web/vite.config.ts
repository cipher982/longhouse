import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Dev proxy: when a `~/.longhouse/machine/device-token` exists alongside a
 * `~/.longhouse/machine/target-url`, the local UI proxies `/api/*` to that
 * backend and forwards the device token as a Bearer header. The backend's
 * browser-auth dependency accepts device tokens for the token owner.
 *
 * No config file, no env var — driven entirely off existing CLI state.
 */
function loadDevProxy(): { target: string; bearer: string } | null {
  try {
    const home = process.env.HOME || "";
    const target = readFileSync(path.join(home, ".longhouse/machine/target-url"), "utf8").trim();
    const bearer = readFileSync(path.join(home, ".longhouse/machine/device-token"), "utf8").trim();
    if (!target || !bearer) return null;
    return { target, bearer };
  } catch {
    return null;
  }
}

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
      return {
        html: html.replace(/__BUILD_HASH__/g, hash),
        tags: [
          {
            tag: "script",
            attrs: {
              src: `/config.js?v=${hash}`,
            },
            injectTo: "head-prepend",
          },
        ],
      };
    },
  };
}

export default defineConfig(({ mode }) => {
  // Load .env from repo root (monorepo root contains single .env file)
  const repoRoot = path.resolve(import.meta.dirname, "..");
  const rootEnv = loadEnv(mode, repoRoot, "");

  const frontendPort = Number(rootEnv.FRONTEND_PORT || 3000);

  // Use root path for both dev and production
  // The /react/ path was legacy and unnecessary - only frontend runs on this port
  const basePath = "/";

  // Proxy target priority:
  //   1. VITE_PROXY_TARGET shell env — explicit override (used by e2e webServer
  //      and CI, where `process.env.VITE_PROXY_TARGET` is set on the command).
  //   2. ~/.longhouse/machine/{target-url,device-token} — point local UI at
  //      the already-authenticated remote backend used by the CLI/engine.
  //   3. VITE_PROXY_TARGET from the repo `.env` file — historical local-backend
  //      default. Only used when the dev-proxy file is absent.
  //   4. Docker Compose DNS fallback.
  //
  // The shell env wins so test runs / CI pin a deterministic backend, but a
  // line in `.env` like `VITE_PROXY_TARGET=http://localhost:47300` does NOT
  // pre-empt the dev-proxy seam — that's the point of the seam.
  const shellProxyTarget = process.env.VITE_PROXY_TARGET || null;
  const devProxy = shellProxyTarget ? null : loadDevProxy();
  const proxyTarget =
    shellProxyTarget || devProxy?.target || rootEnv.VITE_PROXY_TARGET || "http://backend:8000";

  if (devProxy) {
    console.log(`[dev-proxy] forwarding /api/* to ${devProxy.target} as device-token owner`);
  }

  const remoteProxyConfigure = devProxy
    ? (proxy: { on: (ev: string, cb: (proxyReq: { setHeader: (k: string, v: string) => void }) => void) => void }) => {
        const targetOrigin = new URL(devProxy.target).origin;
        proxy.on("proxyReq", (proxyReq) => {
          proxyReq.setHeader("authorization", `Bearer ${devProxy.bearer}`);
          // changeOrigin rewrites Host to the remote Runtime Host. Keep the
          // forwarded browser origin aligned with that Host so cookie/form
          // CSRF checks and auth refresh/logout work through the dev proxy.
          proxyReq.setHeader("origin", targetOrigin);
        });
      }
    : undefined;

  return {
    plugins: [react(), buildHashPlugin()],
    base: basePath,
    resolve: {
      preserveSymlinks: false,
      // The landing demo's data and TerminalGrid live in video/src/demo, which
      // imports nothing from Remotion. Reach it by path so the default
      // workspace install never pulls Remotion; video/ is its own install
      // (`cd video && bun install`) used only to render. Mirrored in
      // tsconfig.base.json `paths`.
      alias: {
        "@longhouse/video/demo": path.resolve(import.meta.dirname, "../video/src/demo/index.ts"),
      },
      // Prevent React duplication across workspaces/hoisting
      dedupe: ["react", "react-dom", "react/jsx-runtime", "react/jsx-dev-runtime"],
    },
    server: {
      host: "0.0.0.0",
      port: frontendPort,
      // Disable browser caching in dev — stale modules cause ghost UI bugs
      headers: {
        "Cache-Control": "no-store",
      },
      // Enable file watching with polling for Docker volumes
      watch: {
        usePolling: true,
        interval: 1000,
      },
      proxy: {
        "/config.js": {
          target: proxyTarget,
          changeOrigin: true,
          configure: remoteProxyConfigure,
        },
        "/api/ws": {
          target: proxyTarget,
          ws: true,
          changeOrigin: true,
          configure: remoteProxyConfigure,
        },
        "/api": {
          target: proxyTarget,
          changeOrigin: true,
          configure: remoteProxyConfigure,
        },
      },
    },
    build: {
      sourcemap: true,
      outDir: "dist",
      rolldownOptions: {
        output: {
          codeSplitting: {
            groups: [
              {
                // Only the libraries the app shell needs on every page share a
                // long-lived vendor chunk. Every other dependency follows its
                // importer: markdown, syntax highlighting and dnd-kit ride with
                // the lazily loaded session pages, xterm with the lazy landing
                // demo, so none of them reach the landing page's first load.
                name: "vendor",
                test: /[\\/]node_modules[\\/](react|react-dom|scheduler|react-router|@tanstack|react-hot-toast|goober)[\\/]/,
              },
            ],
          },
        },
      },
      // Pages behind the app shell and the landing demos are lazy chunks
      // (routes/App.tsx, pages/LandingPage.tsx); the remaining eager shell is
      // well under this floor, which still catches a regression.
      chunkSizeWarningLimit: 750,
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
    },
  };
});
