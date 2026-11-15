#!/usr/bin/env node
// Longhouse's private Playwright browser store.
//
// Why this exists: Playwright keeps browsers in one per-user cache
// (~/Library/Caches/ms-playwright) guarded by a single directory lock, with
// garbage collection keyed on whichever projects last ran `playwright install`.
// Every repo on the machine shares it, each pinning a different revision, and
// one install that stalls mid-download (no timeout) holds the lock for days
// while every other agent queues behind it. Agents then see "Executable doesn't
// exist" and retry the same blocked install.
//
// What this does instead:
//   - Longhouse browsers live in their own directory, shared by every Longhouse
//     worktree and untouched by other repos (no lock, GC or revision fights).
//   - "Is it installed" is two stat calls against a cached plan: no lock, no
//     network, no installer process.
//   - Installing takes our own lock (heartbeat + owner pid, so a dead or hung
//     owner is reclaimed instead of waited on), runs Playwright's pinned CLI in
//     its own process group with a private TMPDIR, and kills it when it stops
//     making progress. Partial downloads are deleted. Waiting is bounded.
//
// CLI:  node scripts/playwright-store.mjs ensure [chromium|webkit|firefox ...]
//       node scripts/playwright-store.mjs status [names...]   (exit 1 if missing)
//       node scripts/playwright-store.mjs path
// Import: `import "<rel>/playwright-store-auto.mjs"` first in any script that
// launches a browser, or call ensureBrowsersSync([...]) for non-chromium ones.

import { spawn, spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const SELF = fileURLToPath(import.meta.url);
const require = createRequire(import.meta.url);

const num = (name, fallback) => {
  const value = Number.parseInt(process.env[name] ?? "", 10);
  return Number.isFinite(value) && value > 0 ? value : fallback;
};

// All tunable so tests can shrink them; defaults suit a slow home connection.
const limits = () => {
  const total = num("LONGHOUSE_PLAYWRIGHT_TOTAL_MS", 15 * 60_000);
  return {
    idleMs: num("LONGHOUSE_PLAYWRIGHT_IDLE_MS", 180_000), // no bytes, no output
    totalMs: total, // hard cap on one installer run
    waitMs: num("LONGHOUSE_PLAYWRIGHT_WAIT_MS", total + 60_000), // waiting on another installer
    staleMs: num("LONGHOUSE_PLAYWRIGHT_STALE_MS", 60_000), // heartbeat age that marks an owner hung
  };
};

export class StoreError extends Error {}

// ---------------------------------------------------------------------------
// Where things live
// ---------------------------------------------------------------------------

// The real home directory from the passwd database, not $HOME. Sandboxes, the
// isolated test runtime and provider-CLI proofs all point HOME at a scratch
// directory; resolving the store from $HOME would hand each of them an empty
// store and a fresh 250 MB download (and delete it afterwards).
function realHome() {
  try {
    return os.userInfo().homedir || os.homedir();
  } catch {
    return os.homedir();
  }
}

export function defaultStore() {
  const home = realHome();
  if (process.platform === "darwin") return path.join(home, "Library", "Caches", "longhouse", "playwright");
  return path.join(home, ".cache", "longhouse", "playwright");
}

// An explicit PLAYWRIGHT_BROWSERS_PATH (CI cache, the test container's
// /opt/playwright) wins; "0" means "inside node_modules" and gets our store.
export function storeDir() {
  const explicit = process.env.PLAYWRIGHT_BROWSERS_PATH;
  return explicit && explicit !== "0" ? explicit : defaultStore();
}

// Point this process, and every child it spawns, at the store. Must run before
// playwright-core is first imported: it reads the path once at module load.
export function applyStoreEnv() {
  const store = storeDir();
  process.env.PLAYWRIGHT_BROWSERS_PATH = store;
  return store;
}

function playwrightCli() {
  if (process.env.LONGHOUSE_PLAYWRIGHT_CLI) return process.env.LONGHOUSE_PLAYWRIGHT_CLI;
  return path.join(path.dirname(require.resolve("playwright-core/package.json")), "cli.js");
}

function playwrightVersion() {
  if (process.env.LONGHOUSE_PLAYWRIGHT_CLI) return "fake";
  return require("playwright-core/package.json").version;
}

const stateDir = (store) => path.join(store, ".longhouse");
const lockPath = (store) => path.join(stateDir(store), "install.lock");

function planFile(store, names) {
  const key = [playwrightVersion(), process.platform, process.arch, [...names].sort().join("+")].join("-");
  return path.join(stateDir(store), "plans", `${key}.json`);
}

// ---------------------------------------------------------------------------
// Plan: which directories must exist for these browsers
// ---------------------------------------------------------------------------

const markerOf = (dir) => path.join(dir, "INSTALLATION_COMPLETE");
const dirsReady = (dirs) => dirs.length > 0 && dirs.every((dir) => fs.existsSync(markerOf(dir)));

// Playwright's own `install --dry-run` is the public way to ask where a browser
// will be installed for this exact pinned version, host OS and store.
function computePlan(store, names) {
  const result = spawnSync(process.execPath, [playwrightCli(), "install", "--dry-run", ...names], {
    env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: store },
    encoding: "utf8",
    timeout: 60_000,
  });
  if (result.status !== 0) {
    throw new StoreError(`playwright install --dry-run failed (${result.status ?? result.signal}): ${result.stderr || result.stdout}`);
  }
  const dirs = [...new Set([...result.stdout.matchAll(/^\s*Install location:\s+(.+?)\s*$/gm)].map((m) => m[1]))];
  if (dirs.length === 0) {
    throw new StoreError(`could not read install locations from playwright --dry-run for ${names.join(", ")}:\n${result.stdout}`);
  }
  return dirs;
}

function writePlan(store, names, dirs) {
  const file = planFile(store, names);
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify({ names, dirs }));
    fs.renameSync(tmp, file);
  } catch {
    // Read-only store (container image): the plan is just a cache.
  }
}

function readPlan(store, names) {
  try {
    return JSON.parse(fs.readFileSync(planFile(store, names), "utf8")).dirs ?? [];
  } catch {
    return [];
  }
}

/** The fast path: a cached plan and N existence checks. No processes, no lock. */
export function isReadySync(names = ["chromium"], store = storeDir()) {
  return dirsReady(readPlan(store, names));
}

// ---------------------------------------------------------------------------
// Lock: mkdir is atomic; owner pid + heartbeat make dead/hung owners reclaimable
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

const mtimeAge = (file) => {
  try {
    return Date.now() - fs.statSync(file).mtimeMs;
  } catch {
    return Number.POSITIVE_INFINITY;
  }
};

function readOwner(lock) {
  try {
    return JSON.parse(fs.readFileSync(path.join(lock, "owner.json"), "utf8"));
  } catch {
    return null;
  }
}

function inspectLock(lock, staleMs) {
  const owner = readOwner(lock);
  if (!owner) {
    // mkdir succeeded but owner.json is not written yet (or the writer died).
    const age = mtimeAge(lock);
    return { owner: null, stale: age > 10_000, reason: "lock has no owner", age };
  }
  const age = Date.now() - owner.startedAt;
  // A brand-new owner may not have beaten yet: measure from its start, not "never".
  const beatFile = path.join(lock, "heartbeat");
  const heartbeat = fs.existsSync(beatFile) ? mtimeAge(beatFile) : age;
  if (!pidAlive(owner.pid)) return { owner, stale: true, reason: `owner pid ${owner.pid} is dead`, age };
  if (heartbeat > staleMs) {
    return { owner, stale: true, reason: `owner pid ${owner.pid} stopped heartbeating ${Math.round(heartbeat / 1000)}s ago`, age };
  }
  return { owner, stale: false, reason: "", age };
}

// Only signal a pid when its command line still looks like what we recorded,
// so a recycled pid is never killed.
function commandOf(pid) {
  const result = spawnSync("ps", ["-o", "command=", "-p", String(pid)], { encoding: "utf8" });
  return result.status === 0 ? result.stdout.trim() : "";
}

function killGroup(pid, signal) {
  try {
    process.kill(-pid, signal);
  } catch {
    try {
      process.kill(pid, signal);
    } catch {
      /* already gone */
    }
  }
}

function reapOrphans(owner) {
  if (owner?.childPid && /install/.test(commandOf(owner.childPid)) && /cli\.js|playwright/.test(commandOf(owner.childPid))) {
    killGroup(owner.childPid, "SIGKILL");
  }
  if (owner?.pid && owner.pid !== process.pid && /playwright-store/.test(commandOf(owner.pid))) {
    try {
      process.kill(owner.pid, "SIGKILL");
    } catch {
      /* already gone */
    }
  }
}

function describeHolder(state) {
  const owner = state.owner;
  if (!owner) return "an installer that has not recorded itself yet";
  return `pid ${owner.pid} (${owner.argv}), running ${Math.round(state.age / 1000)}s`;
}

async function withInstallLock(store, fn) {
  const { waitMs, staleMs } = limits();
  const lock = lockPath(store);
  fs.mkdirSync(path.dirname(lock), { recursive: true });
  const deadline = Date.now() + waitMs;
  let nextNote = 0;

  for (;;) {
    try {
      fs.mkdirSync(lock);
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      const state = inspectLock(lock, staleMs);
      if (state.stale) {
        const graveyard = `${lock}.stale-${process.pid}-${Date.now()}`;
        try {
          fs.renameSync(lock, graveyard); // atomic: exactly one waiter wins
        } catch {
          continue; // someone else reclaimed it first
        }
        process.stderr.write(`playwright-store: reclaiming install lock (${state.reason})\n`);
        reapOrphans(state.owner);
        fs.rmSync(graveyard, { recursive: true, force: true });
        continue;
      }
      if (Date.now() > deadline) {
        throw new StoreError(
          `gave up after ${Math.round(waitMs / 1000)}s waiting for another Playwright install: ${describeHolder(state)}.\n` +
            `That installer is still alive and heartbeating, so it was not reclaimed. Lock: ${lock}`,
        );
      }
      if (Date.now() >= nextNote) {
        process.stderr.write(`playwright-store: waiting for another install to finish: ${describeHolder(state)}\n`);
        nextNote = Date.now() + 15_000;
      }
      await sleep(250);
      continue;
    }

    const token = randomUUID();
    const owner = { token, pid: process.pid, startedAt: Date.now(), argv: path.basename(SELF) + " " + process.argv.slice(2).join(" "), childPid: null };
    const writeOwner = () => fs.writeFileSync(path.join(lock, "owner.json"), JSON.stringify(owner));
    const beat = () => {
      try {
        fs.writeFileSync(path.join(lock, "heartbeat"), String(Date.now()));
      } catch {
        /* lock was reclaimed from under us */
      }
    };
    beat(); // before owner.json: a visible owner always has a heartbeat
    writeOwner();
    const timer = setInterval(beat, 5_000);
    try {
      return await fn({ recordChild: (pid) => { owner.childPid = pid; writeOwner(); } });
    } finally {
      clearInterval(timer);
      // Release only a lock that is still ours.
      if (readOwner(lock)?.token === token) fs.rmSync(lock, { recursive: true, force: true });
    }
  }
}

// ---------------------------------------------------------------------------
// Installer: bounded, observable, cleans up after itself
// ---------------------------------------------------------------------------

// Which runtime executes Playwright's installer. Playwright 1.57's zip
// extraction never finishes under Node 26.x (the download completes, then the
// process sits idle forever, holding Playwright's lock). Controlled test on
// this machine, same zip: Node 26.12 hung >150s, Node 22.14 took 12s, Bun 9s.
// Bun is the repo's required toolchain, so prefer it; fall back to the
// current Node. LONGHOUSE_PLAYWRIGHT_RUNTIME overrides.
export function installerRuntime() {
  if (process.env.LONGHOUSE_PLAYWRIGHT_RUNTIME) return process.env.LONGHOUSE_PLAYWRIGHT_RUNTIME;
  if (process.versions.bun) return process.execPath;
  const probe = spawnSync("bun", ["--version"], { encoding: "utf8" });
  return probe.status === 0 ? "bun" : process.execPath;
}

function dirBytes(dir) {
  let total = 0;
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return 0;
  }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    try {
      total += entry.isDirectory() ? dirBytes(full) : fs.statSync(full).size;
    } catch {
      /* file vanished mid-scan */
    }
  }
  return total;
}

async function runInstaller(store, names, lockCtx) {
  const { idleMs, totalMs } = limits();
  const tmp = path.join(stateDir(store), `tmp-${process.pid}`);
  fs.rmSync(tmp, { recursive: true, force: true });
  fs.mkdirSync(tmp, { recursive: true });

  const runtime = installerRuntime();
  const child = spawn(runtime, [playwrightCli(), "install", ...names], {
    detached: true, // own process group so the whole tree can be killed
    stdio: ["ignore", "pipe", "pipe"],
    env: {
      ...process.env,
      PLAYWRIGHT_BROWSERS_PATH: store,
      TMPDIR: tmp, // downloads land here, so failures can be deleted
      PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT: "30000",
    },
  });
  lockCtx.recordChild(child.pid);

  let outputBytes = 0;
  const tail = [];
  const onData = (chunk) => {
    outputBytes += chunk.length;
    process.stderr.write(chunk);
    for (const line of chunk.toString().split(/\r?\n/)) if (line.trim()) tail.push(line);
    if (tail.length > 20) tail.splice(0, tail.length - 20);
  };
  child.stdout.on("data", onData);
  child.stderr.on("data", onData);

  const started = Date.now();
  let lastSignature = "";
  let lastProgress = Date.now();
  let killedFor = null;
  const kill = (why) => {
    if (killedFor) return;
    killedFor = why;
    killGroup(child.pid, "SIGTERM");
    setTimeout(() => killGroup(child.pid, "SIGKILL"), 3_000).unref();
  };

  const watchdog = setInterval(() => {
    const signature = `${outputBytes}:${dirBytes(tmp)}`;
    if (signature !== lastSignature) {
      lastSignature = signature;
      lastProgress = Date.now();
    }
    if (Date.now() - lastProgress > idleMs) kill(`no download or output progress for ${Math.round(idleMs / 1000)}s`);
    else if (Date.now() - started > totalMs) kill(`exceeded the ${Math.round(totalMs / 1000)}s install limit`);
  }, 1_000);

  const onSignal = (signal) => {
    kill(`received ${signal}`);
  };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);

  try {
    const code = await new Promise((resolve) => {
      child.on("close", (exitCode, signal) => resolve(exitCode ?? `signal ${signal}`));
      child.on("error", () => resolve("spawn error"));
    });
    if (killedFor) {
      throw new StoreError(
        `Playwright install was killed: ${killedFor} (installer runtime: ${runtime}).\n${tail.join("\n")}\n` +
          `If the download reached 100% and then stalled, extraction is hanging in this runtime; ` +
          `set LONGHOUSE_PLAYWRIGHT_RUNTIME to a different node or bun.`,
      );
    }
    if (code !== 0) throw new StoreError(`Playwright install failed (exit ${code}):\n${tail.join("\n")}`);
  } finally {
    clearInterval(watchdog);
    process.off("SIGINT", onSignal);
    process.off("SIGTERM", onSignal);
    killGroup(child.pid, "SIGKILL"); // nothing from this install outlives it
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

// ---------------------------------------------------------------------------
// ensure
// ---------------------------------------------------------------------------

/** Make sure `names` are installed in the store. Resolves to the store path. */
export async function ensureBrowsers(names = ["chromium"]) {
  const store = applyStoreEnv();
  if (isReadySync(names, store)) return store;

  let dirs = computePlan(store, names);
  if (dirsReady(dirs)) {
    writePlan(store, names, dirs);
    return store;
  }

  process.stderr.write(
    `playwright-store: ${names.join(", ")} not installed for Playwright ${playwrightVersion()}. ` +
      `Installing once into ${store} (shared by every Longhouse worktree).\n`,
  );
  await withInstallLock(store, async (lockCtx) => {
    // Another agent may have finished while we waited: look again before downloading.
    dirs = computePlan(store, names);
    if (dirsReady(dirs)) return;
    // Under our exclusive lock a leftover Playwright dirlock can only be debris.
    const dirlock = path.join(store, "__dirlock");
    if (mtimeAge(dirlock) > limits().staleMs) fs.rmSync(dirlock, { recursive: true, force: true });
    await runInstaller(store, names, lockCtx);
    if (!dirsReady(dirs)) {
      const missing = dirs.filter((dir) => !fs.existsSync(markerOf(dir)));
      throw new StoreError(`Playwright install finished but these are still incomplete: ${missing.join(", ")}`);
    }
  });
  writePlan(store, names, dirs);
  return store;
}

/** Synchronous wrapper for scripts: instant when ready, one visible install otherwise. */
export function ensureBrowsersSync(names = ["chromium"]) {
  const store = applyStoreEnv();
  if (isReadySync(names, store)) return store;
  // No cached plan (first run, or a read-only store like the test image):
  // one dry-run answers "installed?" without spawning an installer.
  const dirs = computePlan(store, names);
  if (dirsReady(dirs)) {
    writePlan(store, names, dirs);
    return store;
  }
  const result = spawnSync(process.execPath, [SELF, "ensure", ...names], { stdio: "inherit", env: process.env });
  if (result.status !== 0) {
    throw new StoreError(`Playwright browsers (${names.join(", ")}) are unavailable; see the message above. Retry with: make ensure-playwright-browser`);
  }
  return store;
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

async function main(argv) {
  const [command = "ensure", ...rest] = argv;
  const names = rest.length > 0 ? rest : ["chromium"];
  if (command === "path") {
    console.log(storeDir());
    return 0;
  }
  if (command === "status") {
    const store = applyStoreEnv();
    const ready = isReadySync(names, store) || dirsReady(computePlan(store, names));
    console.log(JSON.stringify({ store, names, ready }));
    return ready ? 0 : 1;
  }
  if (command === "ensure") {
    await ensureBrowsers(names);
    return 0;
  }
  console.error(`usage: playwright-store.mjs ensure|status|path [chromium|webkit|firefox ...]`);
  return 2;
}

if (process.argv[1] && path.resolve(process.argv[1]) === SELF) {
  main(process.argv.slice(2)).then(
    (code) => process.exit(code),
    (error) => {
      console.error(`playwright-store: ${error instanceof StoreError ? error.message : error.stack}`);
      process.exit(1);
    },
  );
}
