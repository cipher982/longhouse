#!/usr/bin/env node
// Guard the web/src feature-folder layout (control-plane spec
// ui-codebase-cleanup-2026-09, P2). Source lives in app/, features/, shared/,
// generated/, dev/ or embeds/; the old file-type roots (components/, pages/,
// lib/, hooks/, services/, styles/) must not come back.
import { readdirSync, statSync } from "node:fs";
import path from "node:path";

const src = path.resolve(import.meta.dirname, "../src");
const allowed = new Set(["app", "features", "shared", "generated", "dev", "embeds"]);

function hasFiles(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isFile()) return true;
    if (entry.isDirectory() && hasFiles(path.join(dir, entry.name))) return true;
  }
  return false;
}

const offenders = [];
for (const entry of readdirSync(src, { withFileTypes: true })) {
  if (allowed.has(entry.name)) continue;
  const full = path.join(src, entry.name);
  if (entry.isDirectory() && !hasFiles(full)) continue;
  if (entry.name.startsWith(".") && statSync(full).isFile()) continue;
  offenders.push(`web/src/${entry.name}${entry.isDirectory() ? "/" : ""}`);
}

if (offenders.length) {
  console.error("web/src layout: unexpected top-level entries:");
  for (const o of offenders) console.error(`  ${o}`);
  console.error(
    `Put code in ${[...allowed].map((d) => `${d}/`).join(", ")} ` +
      "(features/<name>/ for a feature, shared/ for code several features use).",
  );
  process.exit(1);
}
console.log("web/src layout: OK");
