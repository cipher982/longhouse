import os from "os";
import path from "path";
import { ensureTestRuntime, stripAmbientSecrets } from "./test-runtime.js";

stripAmbientSecrets();
const runtime = ensureTestRuntime();
const probeDir = path.join(runtime.artifactDir, "parallelism-probe");
const cpuCount = Math.max(1, os.cpus()?.length ?? 0);
const envWorkerCount = Number.parseInt(
  process.env.PLAYWRIGHT_WORKERS ?? "",
  10,
);
const workerCount =
  Number.isFinite(envWorkerCount) && envWorkerCount > 0
    ? envWorkerCount
    : process.env.CI
      ? 4
      : cpuCount;

export default {
  testDir: "./probes",
  fullyParallel: true,
  workers: workerCount,
  retries: 0,
  timeout: 30_000,
  outputDir: path.join(probeDir, "test-results"),
  reporter: [["line"]],
  use: {
    baseURL: "http://127.0.0.1:1",
  },
};
