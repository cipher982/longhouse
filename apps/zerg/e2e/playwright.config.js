// Simple, clean Playwright configuration - just read ports from .env
import fs from 'fs';
import path from 'path';
import os from 'os';
import { fileURLToPath } from 'url';
import { devices } from '@playwright/test';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function findDotEnv(startDir) {
  let dir = startDir;
  for (let i = 0; i < 8; i++) {
    const candidate = path.join(dir, '.env');
    if (fs.existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

// Load .env file from repo root (walk upward so this works from any CWD).
const envPath = findDotEnv(__dirname);
let BACKEND_PORT = 8001;
let FRONTEND_PORT = 8002;

function loadDotEnv(filePath) {
  if (!fs.existsSync(filePath)) return;
  const envContent = fs.readFileSync(filePath, 'utf8');
  envContent.split('\n').forEach(rawLine => {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) return;
    const idx = line.indexOf('=');
    if (idx <= 0) return;
    const key = line.slice(0, idx).trim();
    let value = line.slice(idx + 1).trim();
    const isQuoted = (value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"));
    if (!isQuoted) {
      // Strip inline comments for common `.env` style: KEY=value # comment
      value = value.replace(/\s+#.*$/, '').trim();
    }
    if (isQuoted) {
      value = value.slice(1, -1);
    }
    if (process.env[key] === undefined) {
      process.env[key] = value;
    }
  });
}

if (envPath && fs.existsSync(envPath)) {
  // Ensure Playwright-started web servers inherit your repo-root .env.
  loadDotEnv(envPath);

  const envContent = fs.readFileSync(envPath, 'utf8');
  envContent.split('\n').forEach(line => {
    const [key, value] = line.split('=');
    if (key === 'BACKEND_PORT') BACKEND_PORT = parseInt(value) || 8001;
    if (key === 'FRONTEND_PORT') FRONTEND_PORT = parseInt(value) || 8002;
  });
}

// Allow env vars to override
BACKEND_PORT = process.env.BACKEND_PORT ? parseInt(process.env.BACKEND_PORT) : BACKEND_PORT;
FRONTEND_PORT = process.env.FRONTEND_PORT ? parseInt(process.env.FRONTEND_PORT) : FRONTEND_PORT;

const frontendBaseUrl = `http://localhost:${FRONTEND_PORT}`;
process.env.PLAYWRIGHT_FRONTEND_BASE = frontendBaseUrl;

// Define workers count first so we can use it later
// Pinned defaults for reproducible test runs:
// - Local: 16 Playwright workers (pair with 6+ uvicorn workers for zero flake)
// - CI: 4 Playwright workers (conservative for shared runners)
// Override with PLAYWRIGHT_WORKERS env var if needed.
const envWorkers = Number.parseInt(process.env.PLAYWRIGHT_WORKERS ?? "", 10);
const defaultLocalWorkers = 16;  // Tested optimal with 6 uvicorn workers
const defaultCIWorkers = 4;
const workers = Number.isFinite(envWorkers) && envWorkers > 0
  ? envWorkers
  : (process.env.CI ? defaultCIWorkers : defaultLocalWorkers);

const frontendServer = {
  // React dev server for Playwright runs
  // Call vite directly via bunx instead of `bun run dev` to avoid output buffering
  command: `bunx vite --host 127.0.0.1 --port ${FRONTEND_PORT}`,
  port: FRONTEND_PORT,
  reuseExistingServer: !process.env.CI,
  timeout: 180_000,
  cwd: path.resolve(__dirname, '../frontend-web'),
  env: {
    ...process.env,
    VITE_PROXY_TARGET: `http://127.0.0.1:${BACKEND_PORT}`,
    // E2E should bypass auth gating.
    VITE_AUTH_ENABLED: 'false',
  },
};

const config = {
  testDir: './tests',

  use: {
    baseURL: frontendBaseUrl,
    headless: true,
    viewport: { width: 1280, height: 800 },

    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',

    navigationTimeout: 30_000,
    actionTimeout: 10_000,
  },

  globalSetup: './test-setup.js',
  globalTeardown: './test-teardown.js',

  fullyParallel: true,
  workers: workers,
  retries: process.env.CI ? 2 : 1,

  // Reporter configuration: minimal by default, verbose with VERBOSE=1
  // Minimal mode: 3-4 lines stdout, full details in test-results/summary.json
  // Verbose mode: Full Playwright output for debugging
  reporter: process.env.VERBOSE ? [
    ['list'],  // Full test-by-test output
    ['html', { open: 'never' }],
    ['junit', { outputFile: 'test-results/junit.xml' }]
  ] : [
    ['./reporters/minimal-reporter.ts', { outputDir: 'test-results' }],
    ['html', { open: 'never' }],
    ['junit', { outputFile: 'test-results/junit.xml' }]
  ],

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    }
  ],

  webServer: [
    frontendServer,
    {
      // Start a single backend server; DB isolation happens via X-Test-Worker header
      command: `node spawn-test-backend.js`,
      port: BACKEND_PORT,
      cwd: __dirname,
      reuseExistingServer: !process.env.CI, // Allow reusing in development
      timeout: 120_000, // Backend needs time for schema setup
      env: {
        ...process.env,
        BACKEND_PORT: String(BACKEND_PORT),
      },
    },
  ],
};

export default config;
