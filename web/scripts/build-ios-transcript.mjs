#!/usr/bin/env node
// Build the iOS transcript document, or with --check fail when the checked-in
// ios/Resources/Transcript/transcript.html is stale against its web source.
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { build } from "vite";

const webRoot = path.resolve(import.meta.dirname, "..");
const configFile = path.join(webRoot, "vite.transcript.config.ts");
const checkedIn = path.resolve(webRoot, "../ios/Resources/Transcript/transcript.html");
const check = process.argv.includes("--check");

if (!check) {
  await build({ configFile });
  console.log(`wrote ${path.relative(process.cwd(), checkedIn)}`);
  process.exit(0);
}

const scratch = mkdtempSync(path.join(tmpdir(), "ios-transcript-"));
try {
  await build({ configFile, build: { outDir: scratch, emptyOutDir: true } });
  const fresh = readFileSync(path.join(scratch, "transcript.html"), "utf8");
  let current = "";
  try {
    current = readFileSync(checkedIn, "utf8");
  } catch {
    // Missing counts as stale.
  }
  if (fresh !== current) {
    console.error(
      "ios/Resources/Transcript/transcript.html is stale against web/src/embeds/ios-transcript.\n" +
        "Run 'make generate-ios-transcript' and commit the result.",
    );
    process.exit(1);
  }
  console.log("iOS transcript document is current.");
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
