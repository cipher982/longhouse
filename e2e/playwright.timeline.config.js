// Playwright config for profiling parallelism.
// Keeps the normal E2E config but writes a JSON timeline to disk for analysis.

import path from "path";
import baseConfig from "./playwright.config.js";

export default {
  ...baseConfig,
  reporter: [
    ["line"],
    [
      "json",
      {
        outputFile: path.join(
          process.env.E2E_ARTIFACT_DIR ?? "test-results",
          "playwright-timeline.json",
        ),
      },
    ],
  ],
};
