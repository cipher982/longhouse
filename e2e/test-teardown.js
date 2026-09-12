/**
 * Global Playwright teardown.
 *
 * Playwright may still own webServer children while globalTeardown runs, so this
 * hook intentionally does not delete an open SQLite directory or probe/signal a
 * port. The isolated launcher removes the run root after Playwright exits;
 * E2E_ARTIFACT_DIR remains durable for requested reports.
 */

import { ensureTestRuntime } from "./test-runtime.js";

export default async function globalTeardown() {
  const runtime = ensureTestRuntime();
  console.log(`E2E teardown: reports retained in ${runtime.artifactDir}`);
}
