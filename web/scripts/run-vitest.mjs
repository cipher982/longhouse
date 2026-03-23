#!/usr/bin/env node

import { spawn, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const rawArgs = process.argv.slice(2);
const vitestArgs = [];
let shouldForceSingleThread = false;
let hasRunFlag = false;
let isSilent = false;

for (const arg of rawArgs) {
  if (arg === "--runInBand") {
    shouldForceSingleThread = true;
    continue;
  }
  if (arg === "--run") {
    hasRunFlag = true;
  }
  if (arg === "--silent") {
    isSilent = true;
  }
  vitestArgs.push(arg);
}

// Preflight: verify single React installation
const __dirname = dirname(fileURLToPath(import.meta.url));
const verifyScript = resolve(__dirname, "../../../../scripts/verify-single-react.mjs");
const verifyArgs = isSilent ? ["--quiet"] : [];
const verifyResult = spawnSync(process.execPath, [verifyScript, ...verifyArgs], { stdio: "inherit" });
if (verifyResult.status !== 0) {
  console.error("\nReact verification failed. Fix React duplication before running tests.");
  process.exit(1);
}

if (shouldForceSingleThread) {
  vitestArgs.push("--pool=threads");
  vitestArgs.push("--poolOptions.threads.minWorkers=1");
  vitestArgs.push("--poolOptions.threads.maxWorkers=1");
  vitestArgs.push("--sequence.concurrent=false");
}

if (!hasRunFlag) {
  vitestArgs.push("--run");
}

const require = createRequire(import.meta.url);
const vitestPackagePath = require.resolve("vitest/package.json");
const vitestEntrypoint = resolve(dirname(vitestPackagePath), "vitest.mjs");

const child = spawn(process.execPath, [vitestEntrypoint, ...vitestArgs], {
  stdio: "inherit",
  env: process.env,
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});
