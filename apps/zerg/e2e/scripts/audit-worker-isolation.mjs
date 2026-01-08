#!/usr/bin/env node

/**
 * Quick static audit for E2E worker isolation footguns.
 *
 * - Ensures spec files import `test` from `./fixtures` (so X-Test-Worker is injected).
 * - Flags ad-hoc `playwright.request.newContext(...)` calls that don't mention X-Test-Worker.
 * - Flags hardcoded backend URLs like http://localhost:8001 which often bypass the shared helpers.
 *
 * Usage:
 *   node apps/zerg/e2e/scripts/audit-worker-isolation.mjs
 */

import fs from "fs";
import path from "path";

const repoRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../../../..");
const testsRoot = path.join(repoRoot, "apps/zerg/e2e/tests");

function walk(dir) {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...walk(full));
      continue;
    }
    if (entry.isFile() && (full.endsWith(".spec.ts") || full.endsWith(".ts"))) {
      files.push(full);
    }
  }
  return files;
}

function rel(p) {
  return path.relative(repoRoot, p);
}

const files = walk(testsRoot);

const issues = {
  missingFixturesImport: [],
  newContextMissingHeader: [],
  hardcodedBackendUrl: [],
};

for (const file of files) {
  const text = fs.readFileSync(file, "utf8");
  const isSpec = file.endsWith(".spec.ts");

  if (isSpec) {
    const importsFixtures = /from\s+['"]\.\/fixtures['"]/.test(text);
    if (!importsFixtures) {
      issues.missingFixturesImport.push(rel(file));
    }
  }

  // Naive but useful: if a file calls playwright.request.newContext(...) and doesn't mention
  // X-Test-Worker anywhere in the file, it’s very likely creating an un-scoped request context.
  if (text.includes("playwright.request.newContext(") && !text.includes("X-Test-Worker")) {
    issues.newContextMissingHeader.push(rel(file));
  }

  if (/(https?:\/\/(localhost|127\.0\.0\.1):8001)\b/.test(text)) {
    issues.hardcodedBackendUrl.push(rel(file));
  }
}

function printList(title, list) {
  if (!list.length) return;
  console.log(`\n${title} (${list.length})`);
  for (const item of list) console.log(`- ${item}`);
}

console.log(`Scanned ${files.length} files under ${path.relative(repoRoot, testsRoot)}`);
printList("Spec files not importing ./fixtures", issues.missingFixturesImport);
printList("Files creating request.newContext() without X-Test-Worker", issues.newContextMissingHeader);
printList("Files containing hardcoded http://localhost:8001", issues.hardcodedBackendUrl);

const totalIssues =
  issues.missingFixturesImport.length +
  issues.newContextMissingHeader.length +
  issues.hardcodedBackendUrl.length;

if (totalIssues) {
  process.exitCode = 1;
}
