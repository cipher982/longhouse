import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { devices } from "@playwright/test";
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
const artifactDir = path.join(runtime.artifactDir, "playwright");
const outputDir = path.join(artifactDir, "test-results");
fs.mkdirSync(outputDir, { recursive: true });

// Ports supplied by the isolated launcher are stable across Playwright config
// reloads. Direct config use deliberately ignores ambient BACKEND_PORT values:
// stale developer servers must be refused, never silently reused.
const backendPort =
  suppliedIsolatedRuntime && process.env.E2E_BACKEND_PORT
    ? parsePort(process.env.E2E_BACKEND_PORT, "E2E_BACKEND_PORT")
    : randomPort();
let frontendPort =
  suppliedIsolatedRuntime && process.env.E2E_FRONTEND_PORT
    ? parsePort(process.env.E2E_FRONTEND_PORT, "E2E_FRONTEND_PORT")
    : randomPort();
while (frontendPort === backendPort) frontendPort = randomPort();

process.env.BACKEND_PORT = String(backendPort);
process.env.FRONTEND_PORT = String(frontendPort);
process.env.PLAYWRIGHT_FRONTEND_BASE = `http://127.0.0.1:${frontendPort}`;
process.env.NODE_ENV = "test";
process.env.E2E_BACKEND_PORT = String(backendPort);
process.env.E2E_FRONTEND_PORT = String(frontendPort);
process.env.TESTING = "1";

const envWorkerCount = Number.parseInt(
  process.env.PLAYWRIGHT_WORKERS ?? "",
  10,
);
const defaultLocalWorkerCount = 4;
const defaultCIWorkerCount = 4;
const workerCount =
  Number.isFinite(envWorkerCount) && envWorkerCount > 0
    ? envWorkerCount
    : process.env.CI
      ? defaultCIWorkerCount
      : defaultLocalWorkerCount;

const frontendServer = {
  command: `bunx vite --host 127.0.0.1 --port ${frontendPort} --strictPort`,
  port: frontendPort,
  reuseExistingServer: false,
  timeout: 180_000,
  cwd: path.resolve(__dirname, "../web"),
  env: safeChildEnvironment({
    VITE_PROXY_TARGET: `http://127.0.0.1:${backendPort}`,
    VITE_WS_BASE_URL: `ws://127.0.0.1:${backendPort}`,
    VITE_AUTH_ENABLED: "false",
    VITE_E2E: "true",
  }),
};

const reporters = process.env.VERBOSE
  ? [
      ["list"],
      ["html", { open: "never", outputFolder: path.join(artifactDir, "html") }],
      ["junit", { outputFile: path.join(artifactDir, "junit.xml") }],
    ]
  : [
      ["./reporters/minimal-reporter.ts", { outputDir: artifactDir }],
      ["html", { open: "never", outputFolder: path.join(artifactDir, "html") }],
      ["junit", { outputFile: path.join(artifactDir, "junit.xml") }],
    ];

const config = {
  testDir: "./tests",
  outputDir,

  use: {
    baseURL: `http://127.0.0.1:${frontendPort}`,
    headless: true,
    viewport: { width: 1280, height: 800 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    navigationTimeout: 30_000,
    actionTimeout: 10_000,
  },

  expect: {
    toHaveScreenshot: {
      pathTemplate:
        "{testDir}/{testFilePath}-snapshots/{arg}{-projectName}-darwin{ext}",
    },
  },

  globalSetup: "./test-setup.js",
  globalTeardown: "./test-teardown.js",
  fullyParallel: true,
  workers: workerCount,
  retries: process.env.CI ? 2 : 1,
  reporter: reporters,

  projects: [
    {
      name: "core",
      testDir: "./tests/core",
      retries: 0,
      timeout: 60_000,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "chromium",
      testDir: "./tests",
      testIgnore: ["**/core/**", "**/*.test.ts"],
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  webServer: [
    frontendServer,
    {
      command: "node spawn-test-backend.js",
      url: `http://127.0.0.1:${backendPort}/api/health/db`,
      cwd: __dirname,
      reuseExistingServer: false,
      timeout: 120_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
      env: safeChildEnvironment({
        BACKEND_PORT: String(backendPort),
        FRONTEND_PORT: String(frontendPort),
      }),
    },
  ],
};

export default config;
