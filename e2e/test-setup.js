/**
 * Global Playwright setup. The launcher/configuration owns the run identity;
 * setup only verifies and creates directories inside that identity.
 */

import fs from "fs";
import { ensureTestRuntime } from "./test-runtime.js";

export default async function globalSetup() {
  const runtime = ensureTestRuntime();
  fs.mkdirSync(runtime.dbDir, { recursive: true });
  fs.mkdirSync(runtime.artifactDir, { recursive: true });
  console.log(`E2E setup: isolated run root ${runtime.root}`);
}
