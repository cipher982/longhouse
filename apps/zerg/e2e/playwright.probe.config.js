import os from "os";

// Separate Playwright config for fast parallelism experiments.
// Keeps the main E2E suite unchanged (no webServer, no global setup/teardown).

const workers = process.env.CI ? 4 : os.cpus().length;

export default {
  testDir: "./probes",
  fullyParallel: true,
  workers,
  retries: 0,
  timeout: 30_000,
  reporter: [["line"]],
};
