import fs from "fs";
import os from "os";
import path from "path";
import { fileURLToPath } from "url";
import {
  ensureTestRuntime,
  parsePort,
  randomPort,
  safeChildEnvironment,
  stripAmbientSecrets,
} from "./test-runtime.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const suppliedIsolatedRuntime = process.env.LONGHOUSE_TEST_ISOLATED === "1";
stripAmbientSecrets();
const runtime = ensureTestRuntime();
const backendPort =
  suppliedIsolatedRuntime && process.env.E2E_BACKEND_PORT
    ? parsePort(process.env.E2E_BACKEND_PORT, "E2E_BACKEND_PORT")
    : randomPort();
process.env.E2E_BACKEND_PORT = String(backendPort);
process.env.BACKEND_PORT = String(backendPort);
const reportDir = path.join(runtime.artifactDir, "backend-probe");
fs.mkdirSync(reportDir, { recursive: true });

const cpuCount = Math.max(1, os.cpus()?.length ?? 0);
const envWorkerCount = Number.parseInt(
  process.env.PLAYWRIGHT_WORKERS ?? "",
  10,
);
const workerCount =
  Number.isFinite(envWorkerCount) && envWorkerCount > 0
    ? envWorkerCount
    : process.env.CI
      ? 4
      : cpuCount;

export default {
  testDir: "./probes",
  testMatch: ["**/*backend_parallelism.probe.spec.ts"],
  fullyParallel: true,
  workers: workerCount,
  retries: 0,
  timeout: 30_000,
  outputDir: path.join(reportDir, "test-results"),
  reporter: [
    ["line"],
    [
      "json",
      { outputFile: path.join(reportDir, "backend-probe-timeline.json") },
    ],
  ],
  webServer: [
    {
      command: "node spawn-test-backend.js",
      url: `http://127.0.0.1:${backendPort}/api/health/db`,
      port: backendPort,
      cwd: __dirname,
      reuseExistingServer: false,
      timeout: 60_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
      env: safeChildEnvironment({
        BACKEND_PORT: String(backendPort),
        E2E_BACKEND_PORT: String(backendPort),
      }),
    },
  ],
};
