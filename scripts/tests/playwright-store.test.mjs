// Behavioural checks for scripts/playwright-store.mjs: the properties that keep
// agents from blocking each other on a browser install. Run: node --test
// scripts/tests/playwright-store.test.mjs   (make validate-playwright-install)
import { spawn, spawnSync, execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const STORE_CLI = path.join(ROOT, "scripts/playwright-store.mjs");
const FAKE_CLI = path.join(ROOT, "scripts/tests/fixtures/fake-playwright-cli.mjs");

function sandbox(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pw-store-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const store = path.join(dir, "store");
  const log = path.join(dir, "installs.log");
  fs.writeFileSync(log, "");
  const env = {
    ...process.env,
    PLAYWRIGHT_BROWSERS_PATH: store,
    LONGHOUSE_PLAYWRIGHT_CLI: FAKE_CLI,
    FAKE_LOG: log,
    FAKE_PIDFILE: path.join(dir, "installer.pid"),
    LONGHOUSE_PLAYWRIGHT_STALE_MS: "2000",
  };
  const installs = () => fs.readFileSync(log, "utf8").split("\n").filter(Boolean).length;
  return { dir, store, env, installs, lock: path.join(store, ".longhouse/install.lock") };
}

function run(env, ...args) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn(process.execPath, [STORE_CLI, ...args], { env, stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    child.stdout.on("data", (c) => (out += c));
    child.stderr.on("data", (c) => (out += c));
    child.on("close", (code) => resolve({ code, out, ms: Date.now() - started }));
  });
}

const alive = (pid) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};

test("first ensure installs once; the second is a no-op that spawns nothing", async (t) => {
  const s = sandbox(t);
  const first = await run(s.env, "ensure", "chromium");
  assert.equal(first.code, 0, first.out);
  assert.equal(s.installs(), 1);

  const second = await run(s.env, "ensure", "chromium");
  assert.equal(second.code, 0, second.out);
  assert.equal(s.installs(), 1, "ready store must not invoke the installer again");
  assert.ok(!fs.existsSync(s.lock), "ready store must not take the lock");
});

test("eight agents ensuring at once cause exactly one install", async (t) => {
  const s = sandbox(t);
  const results = await Promise.all(Array.from({ length: 8 }, () => run(s.env, "ensure", "chromium")));
  for (const r of results) assert.equal(r.code, 0, r.out);
  assert.equal(s.installs(), 1);
  assert.ok(!fs.existsSync(s.lock), "lock released");
});

test("a stalled install is killed, its partial download deleted, the lock released", { timeout: 40_000 }, async (t) => {
  const s = sandbox(t);
  const env = { ...s.env, FAKE_MODE: "hang", LONGHOUSE_PLAYWRIGHT_IDLE_MS: "2500" };
  const result = await run(env, "ensure", "chromium");
  assert.equal(result.code, 1);
  assert.match(result.out, /no download or output progress/);
  assert.ok(result.ms < 15_000, `bounded, took ${result.ms}ms`);

  const pid = Number(fs.readFileSync(path.join(s.dir, "installer.pid"), "utf8"));
  assert.ok(!alive(pid), "installer process group must be dead");
  assert.ok(!fs.existsSync(s.lock), "lock released after failure");
  const leftovers = fs.readdirSync(path.join(s.store, ".longhouse")).filter((n) => n.startsWith("tmp-"));
  assert.deepEqual(leftovers, [], "partial downloads removed");

  // Failure must not poison the store: the next attempt succeeds.
  const retry = await run({ ...s.env, FAKE_MODE: "ok" }, "ensure", "chromium");
  assert.equal(retry.code, 0, retry.out);
});

test("a failing installer surfaces its output and exit code", async (t) => {
  const s = sandbox(t);
  const result = await run({ ...s.env, FAKE_MODE: "fail" }, "ensure", "chromium");
  assert.equal(result.code, 1);
  assert.match(result.out, /boom: download failed/);
  assert.ok(!fs.existsSync(s.lock));
});

test("a lock whose owner died is reclaimed immediately", async (t) => {
  const s = sandbox(t);
  fs.mkdirSync(s.lock, { recursive: true });
  fs.writeFileSync(path.join(s.lock, "owner.json"), JSON.stringify({ token: "x", pid: 2 ** 22 - 7, startedAt: Date.now(), argv: "dead", childPid: null }));
  fs.writeFileSync(path.join(s.lock, "heartbeat"), String(Date.now()));
  const result = await run(s.env, "ensure", "chromium");
  assert.equal(result.code, 0, result.out);
  assert.match(result.out, /owner pid \d+ is dead/);
  assert.equal(s.installs(), 1);
});

test("a live owner that stopped heartbeating is reclaimed, and its installer killed", async (t) => {
  const s = sandbox(t);
  const hung = spawn("sleep", ["300"], { detached: true, stdio: "ignore" });
  t.after(() => {
    try {
      process.kill(hung.pid, "SIGKILL");
    } catch {}
  });
  fs.mkdirSync(s.lock, { recursive: true });
  fs.writeFileSync(path.join(s.lock, "owner.json"), JSON.stringify({ token: "x", pid: hung.pid, startedAt: Date.now() - 60_000, argv: "hung", childPid: null }));
  const heartbeat = path.join(s.lock, "heartbeat");
  fs.writeFileSync(heartbeat, "old");
  fs.utimesSync(heartbeat, new Date(Date.now() - 30_000), new Date(Date.now() - 30_000));
  const result = await run(s.env, "ensure", "chromium");
  assert.equal(result.code, 0, result.out);
  assert.match(result.out, /stopped heartbeating/);
});

test("a live, heartbeating owner is waited on for a bounded time, never stolen", { timeout: 40_000 }, async (t) => {
  const s = sandbox(t);
  const holder = spawn("sleep", ["300"], { stdio: "ignore" });
  t.after(() => holder.kill("SIGKILL"));
  fs.mkdirSync(s.lock, { recursive: true });
  fs.writeFileSync(path.join(s.lock, "owner.json"), JSON.stringify({ token: "keep", pid: holder.pid, startedAt: Date.now(), argv: "installer", childPid: null }));
  const beat = setInterval(() => fs.writeFileSync(path.join(s.lock, "heartbeat"), String(Date.now())), 200);
  t.after(() => clearInterval(beat));
  fs.writeFileSync(path.join(s.lock, "heartbeat"), String(Date.now()));

  const result = await run({ ...s.env, LONGHOUSE_PLAYWRIGHT_WAIT_MS: "1500" }, "ensure", "chromium");
  assert.equal(result.code, 1);
  assert.match(result.out, new RegExp(`pid ${holder.pid}`), "names the holder");
  assert.ok(result.ms < 8_000, `bounded, took ${result.ms}ms`);
  assert.equal(JSON.parse(fs.readFileSync(path.join(s.lock, "owner.json"), "utf8")).token, "keep", "lock untouched");
  assert.equal(s.installs(), 0);
});

test("installs run under bun when it exists (Node 26 hangs extracting Playwright zips)", () => {
  const probe = `import { installerRuntime } from ${JSON.stringify(STORE_CLI)}; console.log(installerRuntime());`;
  const env = { ...process.env };
  delete env.LONGHOUSE_PLAYWRIGHT_RUNTIME;
  const hasBun = spawnSync("bun", ["--version"]).status === 0;
  const chosen = execFileSync(process.execPath, ["--input-type=module", "-e", probe], { env, encoding: "utf8" }).trim();
  assert.equal(chosen, hasBun ? "bun" : process.execPath);
  const forced = execFileSync(process.execPath, ["--input-type=module", "-e", probe], { env: { ...env, LONGHOUSE_PLAYWRIGHT_RUNTIME: "/x/node" }, encoding: "utf8" }).trim();
  assert.equal(forced, "/x/node", "explicit override wins");
});

test("the store is private by default and an explicit path is respected", () => {
  const env = { ...process.env };
  delete env.PLAYWRIGHT_BROWSERS_PATH;
  const probe = `import { storeDir } from ${JSON.stringify(STORE_CLI)}; console.log(storeDir());`;
  const dflt = execFileSync(process.execPath, ["--input-type=module", "-e", probe], { env, encoding: "utf8" }).trim();
  assert.match(dflt, /longhouse[\\/]playwright$/);
  assert.doesNotMatch(dflt, /ms-playwright/, "must not share Playwright's global cache");
  const explicit = execFileSync(process.execPath, ["--input-type=module", "-e", probe], { env: { ...env, PLAYWRIGHT_BROWSERS_PATH: "/opt/playwright" }, encoding: "utf8" }).trim();
  assert.equal(explicit, "/opt/playwright");
});

test("a scratch HOME does not get its own empty store", () => {
  const env = { ...process.env, HOME: "/nonexistent/scratch-home", XDG_CACHE_HOME: "/nonexistent/scratch-cache" };
  delete env.PLAYWRIGHT_BROWSERS_PATH;
  const probe = `import { storeDir } from ${JSON.stringify(STORE_CLI)}; console.log(storeDir());`;
  const chosen = execFileSync(process.execPath, ["--input-type=module", "-e", probe], { env, encoding: "utf8" }).trim();
  assert.ok(chosen.startsWith(os.userInfo().homedir), `store ${chosen} must live under the real home`);
  assert.doesNotMatch(chosen, /nonexistent/);
});

test("a read-only, already-populated store (the test container image) is a no-op", async (t) => {
  if (process.getuid?.() === 0) return t.skip("root ignores file modes");
  const s = sandbox(t);
  assert.equal((await run(s.env, "ensure", "chromium")).code, 0);
  fs.rmSync(path.join(s.store, ".longhouse"), { recursive: true, force: true }); // no plan cache, like the image
  fs.chmodSync(s.store, 0o555);
  try {
    const before = s.installs();
    const result = await run(s.env, "ensure", "chromium");
    assert.equal(result.code, 0, result.out);
    assert.equal(s.installs(), before, "must not try to install into a read-only store");
  } finally {
    fs.chmodSync(s.store, 0o755); // before the sandbox cleanup hook removes the tree
  }
});

test("real playwright-core: dry-run parsing finds the pinned revision's directories", (t) => {
  const core = path.join(ROOT, "node_modules/playwright-core/cli.js");
  if (!fs.existsSync(core)) return t.skip("run bun install first");
  const store = fs.mkdtempSync(path.join(os.tmpdir(), "pw-store-real-"));
  t.after(() => fs.rmSync(store, { recursive: true, force: true }));
  const env = { ...process.env, PLAYWRIGHT_BROWSERS_PATH: store };
  delete env.LONGHOUSE_PLAYWRIGHT_CLI;
  const status = spawnSync(process.execPath, [STORE_CLI, "status", "chromium"], { env, encoding: "utf8" });
  assert.equal(status.status, 1, "an empty store is not ready");
  const parsed = JSON.parse(status.stdout);
  assert.equal(parsed.ready, false);
  assert.equal(parsed.store, store);
});

test("every script and config that launches a browser goes through the store", () => {
  const files = execFileSync("git", ["ls-files", "-co", "--exclude-standard"], { cwd: ROOT, encoding: "utf8" }).split("\n").filter(Boolean);
  const imports = /(from\s+['"](@playwright\/test|playwright|playwright-core)['"])|require\(['"](playwright|@playwright\/test|playwright-core)['"]\)/;
  const skip = /(\.spec\.|\/tests?\/|\/specs?\/|node_modules|^scripts\/playwright-store)/;
  const missing = [];
  for (const rel of files) {
    if (!/\.(ts|mjs|js)$/.test(rel) || skip.test(rel) || !fs.existsSync(path.join(ROOT, rel))) continue;
    const src = fs.readFileSync(path.join(ROOT, rel), "utf8");
    if (imports.test(src) && !/playwright-store-auto(-webkit)?\.mjs/.test(src)) missing.push(rel);
  }
  assert.deepEqual(missing, [], `these import Playwright without ../playwright-store-auto.mjs, so they bypass the private store and its install lock:\n${missing.join("\n")}`);
});
