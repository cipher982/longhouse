import path from "path";
import { devices } from "@playwright/test";
import { ensureTestRuntime, stripAmbientSecrets } from "./test-runtime.js";

stripAmbientSecrets();
const runtime = ensureTestRuntime();
const artifactDir = path.join(runtime.artifactDir, "onboarding");
const frontendBaseUrl =
  process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:30080";

process.env.PLAYWRIGHT_BASE_URL = frontendBaseUrl;

const config = {
  testDir: "./tests/onboarding",
  outputDir: path.join(artifactDir, "test-results"),
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,

  use: {
    baseURL: frontendBaseUrl,
    headless: true,
    viewport: { width: 1280, height: 800 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    navigationTimeout: 45_000,
    actionTimeout: 20_000,
  },

  reporter: process.env.VERBOSE
    ? [
        ["list"],
        [
          "html",
          { open: "never", outputFolder: path.join(artifactDir, "html") },
        ],
        ["junit", { outputFile: path.join(artifactDir, "junit.xml") }],
      ]
    : [
        ["./reporters/minimal-reporter.ts", { outputDir: artifactDir }],
        [
          "html",
          { open: "never", outputFolder: path.join(artifactDir, "html") },
        ],
        ["junit", { outputFile: path.join(artifactDir, "junit.xml") }],
      ],

  projects: [
    {
      name: "onboarding-chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "onboarding-firefox",
      use: { ...devices["Desktop Firefox"] },
    },
    {
      name: "onboarding-webkit",
      use: { ...devices["Desktop Safari"] },
    },
    {
      name: "onboarding-mobile-safari",
      testIgnore: ["**/onboarding_funnel.spec.ts"],
      use: { ...devices["iPhone 13"] },
    },
    {
      name: "onboarding-mobile-chrome",
      testIgnore: ["**/onboarding_funnel.spec.ts"],
      use: { ...devices["Pixel 5"] },
    },
  ],
};

export default config;
