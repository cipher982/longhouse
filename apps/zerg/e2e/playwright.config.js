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
    // Skip port vars - E2E generates its own random ports unless explicitly overridden
    if (key === 'BACKEND_PORT' || key === 'FRONTEND_PORT') return;
    if (process.env[key] === undefined) {
      process.env[key] = value;
    }
  });
}

if (envPath && fs.existsSync(envPath)) {
  loadDotEnv(envPath);
}

// Generate random high ports for E2E - avoids conflicts with dev servers and parallel worktrees
// Cache file is keyed by directory hash so parallel runs in different dirs don't collide
const dirHash = Buffer.from(__dirname).toString('base64').replace(/[^a-zA-Z0-9]/g, '').slice(0, 12);
const portCacheFile = path.join(os.tmpdir(), `pw-ports-${dirHash}.json`);

function getRandomPorts() {
  // Check if ports were already generated (within last 10 min to handle stale caches)
  if (fs.existsSync(portCacheFile)) {
    try {
      const stat = fs.statSync(portCacheFile);
      const ageMs = Date.now() - stat.mtimeMs;
      if (ageMs < 10 * 60 * 1000) { // 10 minutes
        const cached = JSON.parse(fs.readFileSync(portCacheFile, 'utf8'));
        if (cached.backend && cached.frontend) {
          return { backend: cached.backend, frontend: cached.frontend };
        }
      }
    } catch { /* regenerate if cache is corrupt */ }
  }

  // Generate new random ports (range: 30000-59999)
  const randomBase = 30000 + Math.floor(Math.random() * 30000);
  const ports = { backend: randomBase, frontend: randomBase + 1 };

  // Cache for other workers/reloads
  fs.writeFileSync(portCacheFile, JSON.stringify(ports));
  return ports;
}

// Port priority: explicit env var > random generation
// E2E_BACKEND_PORT/E2E_FRONTEND_PORT or BACKEND_PORT/FRONTEND_PORT override random
const randomPorts = getRandomPorts();
let BACKEND_PORT = process.env.E2E_BACKEND_PORT ? parseInt(process.env.E2E_BACKEND_PORT)
  : (process.env.BACKEND_PORT ? parseInt(process.env.BACKEND_PORT) : randomPorts.backend);
let FRONTEND_PORT = process.env.E2E_FRONTEND_PORT ? parseInt(process.env.E2E_FRONTEND_PORT)
  : (process.env.FRONTEND_PORT ? parseInt(process.env.FRONTEND_PORT) : randomPorts.frontend);

// Export to env so fixtures.ts and spawn-test-backend.js can access them
process.env.BACKEND_PORT = String(BACKEND_PORT);
process.env.FRONTEND_PORT = String(FRONTEND_PORT);

const frontendBaseUrl = `http://localhost:${FRONTEND_PORT}`;
process.env.PLAYWRIGHT_FRONTEND_BASE = frontendBaseUrl;

// Define commis count first so we can use it later
// Pinned defaults for reproducible test runs:
// - Local: 4 Playwright commis (more stable with remote Postgres + shared runners)
// - CI: 4 Playwright commis (conservative for shared runners)
// Higher commis counts cause lock contention during parallel DB resets.
// Override with PLAYWRIGHT_WORKERS env var if needed.
const envCommis = Number.parseInt(process.env.PLAYWRIGHT_WORKERS ?? "", 10);
const defaultLocalCommis = 4;  // Lower contention for remote Postgres
const defaultCICommis = 4;
const commis = Number.isFinite(envCommis) && envCommis > 0
  ? envCommis
  : (process.env.CI ? defaultCICommis : defaultLocalCommis);

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
    VITE_WS_BASE_URL: `ws://127.0.0.1:${BACKEND_PORT}`,
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
  workers: commis,
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
    // Core suite: Critical path tests only, no retries allowed
    // Run with: make test-e2e-core or bunx playwright test --project=core
    {
      name: 'core',
      testDir: './tests/core',
      retries: 0,  // Core suite must pass on first try
      timeout: 60000,
      use: { ...devices['Desktop Chrome'] },
    },
    // Full suite: All non-core tests, with retries (core suite has its own project with retries=0)
    // Run with: make test-e2e or bunx playwright test --project=chromium
    {
      name: 'chromium',
      testDir: './tests',
      testIgnore: ['**/core/**'],
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'mobile',
      testDir: './tests/mobile',
      use: { ...devices['iPhone 13'] },
    },
    {
      name: 'mobile-small',
      testDir: './tests/mobile',
      use: { ...devices['iPhone SE'] },
    },
  ],

  webServer: [
    frontendServer,
    {
      // Start a single backend server; DB isolation happens via X-Test-Commis header
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
