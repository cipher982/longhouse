import { devices } from '@playwright/test';

const frontendBaseUrl = process.env.PLAYWRIGHT_BASE_URL || process.env.E2E_FRONTEND_URL || 'https://swarmlet.com';
const apiBaseUrl = process.env.PLAYWRIGHT_API_BASE_URL || process.env.E2E_API_URL || 'https://api.swarmlet.com';

// Expose to tests (fixtures read these env vars).
process.env.PLAYWRIGHT_BASE_URL = frontendBaseUrl;
process.env.PLAYWRIGHT_API_BASE_URL = apiBaseUrl;

const config = {
  testDir: './tests/live',
  fullyParallel: false,
  workers: 1,
  retries: 0,

  use: {
    baseURL: frontendBaseUrl,
    headless: true,
    viewport: { width: 1280, height: 800 },

    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',

    navigationTimeout: 45_000,
    actionTimeout: 20_000,
  },

  reporter: process.env.VERBOSE ? [
    ['list'],
    ['html', { open: 'never' }],
    ['junit', { outputFile: 'test-results/junit.prod.xml' }]
  ] : [
    ['./reporters/minimal-reporter.ts', { outputDir: 'test-results' }],
    ['html', { open: 'never' }],
    ['junit', { outputFile: 'test-results/junit.prod.xml' }]
  ],

  projects: [
    {
      name: 'prod-chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
};

export default config;
