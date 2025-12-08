// Simple, clean Playwright configuration - just read ports from .env
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { devices } from '@playwright/test';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Load .env file from repo root
const envPath = path.resolve(__dirname, '../.env');
let BACKEND_PORT = 8001;
let FRONTEND_PORT = 8002;

if (fs.existsSync(envPath)) {
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
const workers = process.env.CI ? 4 : 2;

const frontendServer = {
  // React dev server for Playwright runs
  command: `npm run dev -- --host 127.0.0.1 --port ${FRONTEND_PORT}`,
  port: FRONTEND_PORT,
  reuseExistingServer: !process.env.CI,
  timeout: 180_000,
  cwd: path.resolve(__dirname, '../frontend-web'),
  env: {
    VITE_PROXY_TARGET: `http://127.0.0.1:${BACKEND_PORT}`,
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

  reporter: [
    ['dot'],  // Concise output for make test - use 'list' for verbose debugging
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
      command: `BACKEND_PORT=${BACKEND_PORT} node spawn-test-backend.js`,
      port: BACKEND_PORT,
      cwd: __dirname,
      reuseExistingServer: !process.env.CI, // Allow reusing in development
      timeout: 60_000,
    },
  ],
};

export default config;
