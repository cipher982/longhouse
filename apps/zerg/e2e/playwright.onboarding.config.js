import { devices } from '@playwright/test';

const frontendBaseUrl = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:30080';

process.env.PLAYWRIGHT_BASE_URL = frontendBaseUrl;

const config = {
  testDir: './tests/onboarding',
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
    ['junit', { outputFile: 'test-results/junit.onboarding.xml' }]
  ] : [
    ['./reporters/minimal-reporter.ts', { outputDir: 'test-results' }],
    ['html', { open: 'never' }],
    ['junit', { outputFile: 'test-results/junit.onboarding.xml' }]
  ],

  projects: [
    {
      name: 'onboarding-chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
};

export default config;
