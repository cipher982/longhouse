import path from "node:path";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const rootEnv = loadEnv(mode, path.resolve(__dirname, ".."), "");

  const backendHost = rootEnv.BACKEND_HOST || "127.0.0.1";
  const backendPort = Number(rootEnv.BACKEND_PORT || 8001);
  const frontendPort = Number(rootEnv.FRONTEND_PORT || 3000);

  // Use root path in production, /react/ in development
  const basePath = mode === "production" ? "/" : "/react/";

  return {
    plugins: [react()],
    base: basePath,
    server: {
      host: "127.0.0.1",
      port: frontendPort,
      proxy: {
        "/api": {
          target: `http://${backendHost}:${backendPort}`,
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
