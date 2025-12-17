#!/usr/bin/env node
/**
 * verify-single-react.mjs
 *
 * Ensures the repo has exactly one React installation for app code.
 * Fails CI/precommit if React duplication or version drift is detected.
 */

import { createRequire } from "node:module";
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");

// Expected pinned version (should match root package.json overrides)
const EXPECTED_REACT_VERSION = "19.2.1";

const require = createRequire(import.meta.url);

let errors = [];
let warnings = [];

console.log("Verifying single React installation...\n");

// 1. Verify React resolution paths
console.log("1. Checking React resolution paths:");
try {
  const reactPath = require.resolve("react");
  const reactDomPath = require.resolve("react-dom");
  const jsxRuntimePath = require.resolve("react/jsx-runtime");

  console.log(`   react:            ${reactPath}`);
  console.log(`   react-dom:        ${reactDomPath}`);
  console.log(`   react/jsx-runtime: ${jsxRuntimePath}`);

  // Verify all resolve to the same node_modules
  const reactDir = dirname(dirname(reactPath));
  const reactDomDir = dirname(dirname(reactDomPath));

  if (reactDir !== reactDomDir) {
    errors.push(
      `react and react-dom resolve to different locations:\n  react: ${reactDir}\n  react-dom: ${reactDomDir}`
    );
  }
} catch (e) {
  errors.push(`Failed to resolve React: ${e.message}`);
}

// 2. Verify React version matches pinned version
console.log("\n2. Checking React versions:");
try {
  const reactPkg = require("react/package.json");
  const reactDomPkg = require("react-dom/package.json");

  console.log(`   react:     ${reactPkg.version}`);
  console.log(`   react-dom: ${reactDomPkg.version}`);
  console.log(`   expected:  ${EXPECTED_REACT_VERSION}`);

  if (reactPkg.version !== EXPECTED_REACT_VERSION) {
    errors.push(
      `react version ${reactPkg.version} does not match pinned version ${EXPECTED_REACT_VERSION}`
    );
  }
  if (reactDomPkg.version !== EXPECTED_REACT_VERSION) {
    errors.push(
      `react-dom version ${reactDomPkg.version} does not match pinned version ${EXPECTED_REACT_VERSION}`
    );
  }
} catch (e) {
  errors.push(`Failed to read React package versions: ${e.message}`);
}

// 3. Scan for workspace-local React installs (these are always bad)
console.log("\n3. Scanning for workspace-local React installations:");
try {
  const findResult = execSync(
    'find apps packages -path "*/node_modules/react/package.json" -o -path "*/node_modules/react-dom/package.json" 2>/dev/null || true',
    { cwd: ROOT, encoding: "utf-8" }
  ).trim();

  const badPaths = findResult.split("\n").filter(Boolean);

  if (badPaths.length > 0) {
    console.log(`   Found ${badPaths.length} forbidden workspace-local install(s):`);
    badPaths.forEach((p) => console.log(`     - ${p}`));
    errors.push(
      "Workspace-local React installs detected. Delete the listed node_modules and run `bun install` from repo root."
    );
  } else {
    console.log("   OK (no workspace-local React installs found)");
  }
} catch (e) {
  warnings.push(`Could not scan for workspace-local React installs: ${e.message}`);
}

// 3b. Verify workspace resolution matches root
console.log("\n3b. Verifying workspace resolution:");
try {
  const workspaces = [
    { name: "zerg-frontend", path: "apps/zerg/frontend-web/package.json" },
    { name: "jarvis-web", path: "apps/jarvis/apps/web/package.json" },
  ];

  for (const ws of workspaces) {
    const wsRequire = createRequire(join(ROOT, ws.path));
    const resolvedPath = wsRequire.resolve("react");
    const expectedPrefix = join(ROOT, "node_modules/react/");

    if (!resolvedPath.startsWith(expectedPrefix)) {
      errors.push(`${ws.name} resolves react to unexpected location: ${resolvedPath}`);
    } else {
      console.log(`   ${ws.name}: OK (resolves to root node_modules)`);
    }
  }
} catch (e) {
  warnings.push(`Could not verify workspace resolution: ${e.message}`);
}

// 4. Verify root package.json has overrides
console.log("\n4. Checking root package.json overrides:");
try {
  const rootPkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf-8"));
  const overrides = rootPkg.overrides || {};

  const hasReactOverride = overrides.react === EXPECTED_REACT_VERSION;
  const hasReactDomOverride = overrides["react-dom"] === EXPECTED_REACT_VERSION;

  console.log(`   react override:     ${overrides.react || "(missing)"}`);
  console.log(`   react-dom override: ${overrides["react-dom"] || "(missing)"}`);

  if (!hasReactOverride) {
    errors.push(
      `Missing or incorrect react override in root package.json. ` +
        `Expected: "${EXPECTED_REACT_VERSION}", got: "${overrides.react || "(none)"}"`
    );
  }
  if (!hasReactDomOverride) {
    errors.push(
      `Missing or incorrect react-dom override in root package.json. ` +
        `Expected: "${EXPECTED_REACT_VERSION}", got: "${overrides["react-dom"] || "(none)"}"`
    );
  }
} catch (e) {
  errors.push(`Failed to read root package.json: ${e.message}`);
}

// Summary
console.log("\n" + "=".repeat(60));
if (warnings.length > 0) {
  console.log("\nWarnings:");
  warnings.forEach((w) => console.log(`  - ${w}`));
}

if (errors.length > 0) {
  console.log("\nErrors:");
  errors.forEach((e) => console.log(`  - ${e}`));
  console.log("\nReact verification FAILED");
  process.exit(1);
} else {
  console.log("\nReact verification PASSED");
  process.exit(0);
}
