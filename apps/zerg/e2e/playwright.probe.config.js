import os from "os";

// Separate Playwright config for fast parallelism experiments.
// Keeps the main E2E suite unchanged (no webServer, no global setup/teardown).

const cpuCount = Math.max(1, os.cpus()?.length ?? 0);
const envWorkers = Number.parseInt(process.env.PLAYWRIGHT_WORKERS ?? "", 10);
const workers = Number.isFinite(envWorkers) && envWorkers > 0 ? envWorkers : (process.env.CI ? 4 : cpuCount);

export default {
  testDir: "./probes",
  fullyParallel: true,
  workers,
  retries: 0,
  timeout: 30_000,
  reporter: [["line"]],
};
