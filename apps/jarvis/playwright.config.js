import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright Configuration for Jarvis E2E Tests
 *
 * Runs inside Docker container via docker-compose.
 * BASE_URL is set by docker-compose environment.
 * Service lifecycle managed by docker-compose (no globalSetup/globalTeardown needed).
 */

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? 'github' : 'list',
  timeout: 60000,

  // Output directories (mounted as volumes in docker-compose)
  outputDir: './test-results',

  use: {
    // BASE_URL set by docker-compose environment
    baseURL: process.env.BASE_URL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'on-first-retry',
  },

  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        permissions: ['microphone'],
      },
    },
    // Disable mobile/webkit for now - focus on chromium
    // {
    //   name: 'mobile',
    //   use: { ...devices['iPhone 13'] },
    // },
    // {
    //   name: 'webkit',
    //   use: { ...devices['Desktop Safari'] },
    // },
  ],

  // No webServer - docker-compose handles service lifecycle
  // No globalSetup/globalTeardown - docker-compose depends_on handles orchestration
});
