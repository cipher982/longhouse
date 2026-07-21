import { devices } from '@playwright/test';

const frontendBaseUrl = process.env.PLAYWRIGHT_BASE_URL || process.env.E2E_FRONTEND_URL || 'https://longhouse.ai';
const apiBaseUrl = process.env.PLAYWRIGHT_API_BASE_URL || process.env.E2E_API_URL || 'https://api.longhouse.ai';
const journeyPrivacyMode = process.env.LONGHOUSE_JOURNEY_PRIVACY_MODE === '1';
const journeyRawOutputDir = process.env.LONGHOUSE_JOURNEY_RAW_OUTPUT_DIR || 'test-results';

// Expose to tests (fixtures read these env vars).
process.env.PLAYWRIGHT_BASE_URL = frontendBaseUrl;
process.env.PLAYWRIGHT_API_BASE_URL = apiBaseUrl;

const config = {
  testDir: './tests/live',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  outputDir: journeyPrivacyMode ? journeyRawOutputDir : 'test-results',

  use: {
    baseURL: frontendBaseUrl,
    headless: true,
    viewport: { width: 1280, height: 800 },

    trace: journeyPrivacyMode ? 'off' : 'retain-on-failure',
    screenshot: journeyPrivacyMode ? 'off' : 'only-on-failure',
    video: journeyPrivacyMode ? 'off' : 'retain-on-failure',

    navigationTimeout: 45_000,
    actionTimeout: 20_000,
  },

  reporter: journeyPrivacyMode ? [
    ['./reporters/privacy-reporter.ts']
  ] : process.env.VERBOSE ? [
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
