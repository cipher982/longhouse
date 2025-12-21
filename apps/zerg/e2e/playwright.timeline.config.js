// Playwright config for profiling parallelism.
// Keeps the normal E2E config but writes a JSON timeline to disk for analysis.

import baseConfig from "./playwright.config.js";

export default {
  ...baseConfig,
  reporter: [
    ["line"],
    ["json", { outputFile: "test-results/playwright-timeline.json" }],
  ],
};
