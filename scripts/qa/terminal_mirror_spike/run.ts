#!/usr/bin/env bun
/**
 * Terminal mirror spike check (terminal-truth-baseline, track C).
 *
 * One command: `bun scripts/qa/terminal_mirror_spike/run.ts`
 *
 * Starts a scratch Runtime Host (loopback, scratch data dir, mirror router
 * enabled), Vite for the lab page, then runs the stand-in TUI through the
 * engine's PTY relay inside a PTY this script owns (the "owner's terminal",
 * recorded byte by byte). Headless Chromium views the mirror from the start,
 * and more viewers join late at checkpoints. At each checkpoint, while the
 * stand-in is paused, every viewer's xterm.js screen text must equal what a
 * separate emulator (@xterm/headless, fed the recorded owner-terminal bytes
 * and resizes) shows. It also reports delay and byte-rate numbers and takes
 * the phone screenshots.
 *
 * Flags: --no-markers (byte-rate run: no latency markers in the stream),
 * --baseline (no host, no browser: relay with no tap, for the relay delay
 * comparison), --out DIR (default scripts/qa/terminal_mirror_spike/results).
 */
import { spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync, appendFileSync } from "node:fs";
import path from "node:path";
import { chromium, type Browser, type Page } from "playwright";
import xtermHeadless from "@xterm/headless";

const { Terminal: HeadlessTerminal } = xtermHeadless as unknown as {
  Terminal: new (o: Record<string, unknown>) => HT;
};
type HT = {
  write(data: string | Uint8Array, cb?: () => void): void;
  resize(c: number, r: number): void;
  cols: number;
  rows: number;
  buffer: { active: { viewportY: number; getLine(y: number): { translateToString(trim: boolean): string } | undefined } };
};

const here = import.meta.dirname;
const repo = path.resolve(here, "../../..");
const args = process.argv.slice(2);
const flag = (name: string) => args.includes(name);
const option = (name: string, fallback: string) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : fallback;
};
const noMarkers = flag("--no-markers");
const baseline = flag("--baseline");
const outDir = path.resolve(option("--out", path.join(here, "results")));
const scratch = `/tmp/agents/terminal-mirror-spike/run-${Date.now()}`;
const OWNER = { cols: 165, rows: 45 };
const AFTER_RESIZE = { cols: 120, rows: 36 };
const sid = "spike";

const children: ChildProcess[] = [];
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const log = (...m: unknown[]) => console.log("[spike]", ...m);

async function freePort(): Promise<number> {
  return new Promise((resolve) => {
    const s = createServer();
    s.listen(0, "127.0.0.1", () => {
      const port = (s.address() as { port: number }).port;
      s.close(() => resolve(port));
    });
  });
}

function start(cmd: string, cmdArgs: string[], opts: { cwd?: string; env?: Record<string, string>; log: string }) {
  const out = path.join(scratch, opts.log);
  const child = spawn(cmd, cmdArgs, {
    cwd: opts.cwd,
    env: { ...process.env, ...opts.env },
    stdio: ["pipe", "pipe", "pipe"],
    detached: true,
  });
  child.stdout?.on("data", (d) => appendFileSync(out, d));
  child.stderr?.on("data", (d) => appendFileSync(out, d));
  children.push(child);
  return child;
}

function killTree(child: ChildProcess) {
  if (child.pid && child.exitCode === null) {
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {
      /* gone */
    }
  }
}

async function waitFor(what: string, check: () => Promise<boolean> | boolean, ms = 90_000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await check()) return;
    await sleep(100);
  }
  throw new Error(`timed out waiting for ${what}`);
}

// ---- reference emulator over the recorded owner-terminal bytes ----------------
type RecordLine = { t: number; d?: string; resize?: [number, number]; exit?: number };
function readRecord(file: string): RecordLine[] {
  if (!existsSync(file)) return [];
  return readFileSync(file, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l));
}

const reference = new HeadlessTerminal({ cols: OWNER.cols, rows: OWNER.rows, scrollback: 0, allowProposedApi: true });
let referenceApplied = 0;
async function advanceReference(file: string) {
  const lines = readRecord(file);
  for (; referenceApplied < lines.length; referenceApplied += 1) {
    const line = lines[referenceApplied];
    if (line.resize) reference.resize(line.resize[0], line.resize[1]);
    if (line.d) await new Promise<void>((r) => reference.write(Buffer.from(line.d!, "base64"), r));
  }
}
function rowsOf(t: HT): string[] {
  const rows: string[] = [];
  for (let y = 0; y < t.rows; y += 1) rows.push(t.buffer.active.getLine(t.buffer.active.viewportY + y)?.translateToString(true) ?? "");
  return rows;
}

// ---- stats -------------------------------------------------------------------
function pct(values: number[], p: number) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))];
}
function dist(values: number[]) {
  const ms = (v: number | null) => (v === null ? null : Math.round(v / 100) / 10);
  return {
    n: values.length,
    p50: ms(pct(values, 50)),
    p90: ms(pct(values, 90)),
    p99: ms(pct(values, 99)),
    max: ms(pct(values, 100)),
  };
}

type Check = { checkpoint: string; viewer: string; expect: "equal" | "known-break"; equal: boolean; cols: number; rows: number; refCols: number; refRows: number; alt?: boolean; firstDiff?: string[]; joinMs?: number };
const checks: Check[] = [];
const shots: string[] = [];

async function main() {
  mkdirSync(scratch, { recursive: true });
  mkdirSync(path.join(scratch, "home"), { recursive: true });
  mkdirSync(outDir, { recursive: true });
  const record = path.join(scratch, "owner.jsonl");
  const events = path.join(scratch, "events.jsonl");
  const markersFile = path.join(scratch, "markers.jsonl");
  const vtDump = path.join(scratch, "vt100.txt");
  writeFileSync(events, "");
  const engine = execOut("python3", [path.join(repo, "scripts/build/cargo.py"), "artifact", "--profile", "ci-test", "--bin", "longhouse-engine"]).trim();
  if (!existsSync(engine)) throw new Error(`engine binary missing, build it first: ${engine}`);

  let hostPort = 0;
  let vitePort = 0;
  let browser: Browser | null = null;
  if (!baseline) {
    hostPort = await freePort();
    vitePort = await freePort();
    // The host runs from the real HOME (uv's cache lives there) but every
    // Longhouse path points into the scratch directory.
    start("uv", ["run", "uvicorn", "zerg.main:app", "--host", "127.0.0.1", "--port", String(hostPort)], {
      cwd: path.join(repo, "server"),
      log: "host.log",
      env: {
        LONGHOUSE_TERMINAL_MIRROR_SPIKE: "1",
        AUTH_DISABLED: "1",
        FERNET_SECRET: randomBytes(32).toString("base64url") + "=",
        JWT_SECRET: randomBytes(24).toString("hex"),
        DATABASE_URL: `sqlite:///${scratch}/host.db`,
        LONGHOUSE_DATA_DIR: path.join(scratch, "data"),
        LONGHOUSE_HOME: path.join(scratch, "lh"),
      },
    });
    start("bunx", ["vite", "--port", String(vitePort), "--strictPort", "--host", "127.0.0.1"], {
      cwd: path.join(repo, "web"),
      log: "vite.log",
      env: { VITE_PROXY_TARGET: `http://127.0.0.1:${hostPort}`, FRONTEND_PORT: String(vitePort), HOME: path.join(scratch, "home") },
    });
    await waitFor("Runtime Host", async () => (await fetch(`http://127.0.0.1:${hostPort}/api/health`).catch(() => null)) !== null, 180_000);
    await waitFor("Vite", async () => (await fetch(`http://127.0.0.1:${vitePort}/terminal-mirror-lab.html`).catch(() => null))?.ok === true);
    // SPIKE_CHROME names a Chrome/Chromium binary when Playwright's own build is not installed.
    browser = await chromium.launch(process.env.SPIKE_CHROME ? { executablePath: process.env.SPIKE_CHROME } : {});
  }
  const lab = (mode: string, extra = "") => `http://127.0.0.1:${vitePort}/terminal-mirror-lab.html?${extra}sid=${sid}&host=127.0.0.1:${hostPort}&mode=${mode}`;

  const viewers: { name: string; page: Page }[] = [];
  async function openViewer(name: string, mode = "native", viewport = { width: 1700, height: 900 }, dpr = 1, extra = "") {
    const context = await browser!.newContext({ viewport, deviceScaleFactor: dpr });
    const page = await context.newPage();
    page.on("pageerror", (e) => log(`pageerror[${name}]`, e.message));
    await page.goto(lab(mode, extra));
    return page;
  }
  async function settled(page: Page) {
    await waitFor("viewer to go live", async () => (await page.evaluate(() => (window as any).__mirror.state.snapshots)) >= 1, 15_000);
    await page.evaluate(() => (window as any).__mirror.idle());
  }
  async function compare(checkpoint: string, name: string, page: Page, expect: Check["expect"] = "equal", joinMs?: number) {
    await page.evaluate(() => (window as any).__mirror.idle());
    const got = await page.evaluate(() => {
      const m = (window as any).__mirror;
      return { rows: m.screenRows() as string[], cols: m.term.cols as number, nrows: m.term.rows as number, alt: m.alternate() as boolean };
    });
    const want = rowsOf(reference);
    const sameSize = got.cols === reference.cols && got.nrows === reference.rows;
    const diffs: string[] = [];
    for (let y = 0; y < Math.max(got.rows.length, want.length); y += 1) {
      if ((got.rows[y] ?? "") !== (want[y] ?? "")) diffs.push(`row ${y}: browser=${JSON.stringify(got.rows[y])} reference=${JSON.stringify(want[y])}`);
    }
    const equal = sameSize && diffs.length === 0;
    checks.push({ checkpoint, viewer: name, expect, equal, cols: got.cols, rows: got.nrows, refCols: reference.cols, refRows: reference.rows, alt: got.alt, firstDiff: equal ? undefined : diffs.slice(0, 3), joinMs });
    log(`${equal ? "EQUAL " : "DIFFER"} ${checkpoint} / ${name} (${got.cols}x${got.nrows}, reference ${reference.cols}x${reference.rows}, alt=${got.alt})`);
  }
  async function lateJoin(checkpoint: string, name: string, expect: Check["expect"] = "equal") {
    const started = Date.now();
    const page = await openViewer(name);
    await settled(page);
    const joinMs = Date.now() - started;
    viewers.push({ name, page });
    await compare(checkpoint, name, page, expect, joinMs);
  }
  async function shot(page: Page, file: string, opts: { fullPage?: boolean; clip?: { x: number; y: number; width: number; height: number } } = {}) {
    const target = path.join(outDir, file);
    await page.screenshot({ path: target, ...opts });
    shots.push(path.relative(repo, target));
  }

  // Main viewer joins before the owner's session starts.
  let main: Page | null = null;
  if (browser) {
    main = await openViewer("viewer-from-start");
    viewers.push({ name: "viewer-from-start", page: main });
  }

  const engineArgs = ["terminal-mirror-spike", "--dump-screen", vtDump, ...(baseline ? [] : ["--mirror-url", `ws://127.0.0.1:${hostPort}/api/terminal-mirror/${sid}/ingest`]), "--", "python3", path.join(here, "standin_tui.py"), "--events", events, "--markers", markersFile, ...(noMarkers ? ["--no-markers"] : [])];
  const owner = start("python3", [path.join(here, "pty_host.py"), "--cols", String(OWNER.cols), "--rows", String(OWNER.rows), "--record", record, "--", engine, ...engineArgs], {
    log: "owner.log",
    env: { HOME: path.join(scratch, "home") },
  });
  log("owner terminal started", `${OWNER.cols}x${OWNER.rows}`);

  const handled = new Set<string>();
  let seenEvents = 0;
  let exited = false;
  const ownerCommand = (line: string) => owner.stdin?.write(line + "\n");

  async function onCheckpoint(name: string) {
    await sleep(900); // let the last frames reach every viewer
    await advanceReference(record);
    if (!browser) return;
    for (const v of viewers) await compare(name, v.name, v.page, (v.name === "late-in-alt-screen" && name === "after-alt") || name === "phone-resize" ? "known-break" : "equal");
    if (name === "after-stream") {
      await lateJoin(name, "late-after-stream");
      // The phone options, at the owner's 165x45.
      const phone = { width: 390, height: 844 };
      for (const mode of ["fit", "native"]) {
        const page = await openViewer(`phone-${mode}`, mode, phone, 2);
        await settled(page);
        await shot(page, `phone-165x45-${mode}.png`);
        if (mode === "native") {
          await page.evaluate(() => document.querySelector(".mirror-viewport")!.scrollTo({ left: 400 }));
          await shot(page, `phone-165x45-native-panned-400px.png`);
        }
        await page.context().close();
      }
      await shot(main!, "desktop-165x45-native.png");
    }
    if (name === "alt-screen") await lateJoin(name, "late-in-alt-screen");
    if (name === "after-alt") {
      await lateJoin(name, "late-after-alt");
      ownerCommand(`resize ${AFTER_RESIZE.cols} ${AFTER_RESIZE.rows}`);
    }
    if (name === "after-resize") {
      await lateJoin(name, "late-after-resize");
      // Type in the browser: keystrokes travel back through the relay's write lock.
      await main!.locator(".xterm-helper-textarea").focus();
      await main!.keyboard.type("hello from the browser");
      await sleep(600);
      await main!.keyboard.press("Enter");
      await sleep(1200);
    }
    if (name === "after-input") {
      const typed = readFileSync(events, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)).find((e) => e.kind === "input");
      checks.push({ checkpoint: "after-input", viewer: "input-through-relay", expect: "equal", equal: typed?.name === "hello from the browser", cols: 0, rows: 0, refCols: 0, refRows: 0, firstDiff: typed ? undefined : ["no input event reached the stand-in"] });
      log(typed?.name === "hello from the browser" ? "EQUAL  input typed in the browser reached the TUI" : "DIFFER input did not reach the TUI");
      // Phone option 3: the phone asks the PTY to resize to its width.
      const phone = { width: 390, height: 844 };
      phonePage = await openViewer("phone-resize", "phone", phone, 2);
      await settled(phonePage);
    }
    if (name === "phone-resize") {
      await sleep(1200);
      await advanceReference(record);
      await shot(phonePage!, "phone-pty-resized-to-phone-viewer.png");
      const phoneSize = await phonePage!.evaluate(() => ({ c: (window as any).__mirror.term.cols, r: (window as any).__mirror.term.rows }));
      // What the owner's own 120x36 terminal received while the PTY was phone-sized.
      const desktop = await openViewer("owner-terminal", "native", { width: 1500, height: 900 }, 1, "sid=none&");
      const lines = readRecord(record);
      const first = lines.find((l) => l.resize)!;
      await desktop.evaluate(([c, r]) => (window as any).__mirror.resize(c, r), [first.resize![0], first.resize![1]]);
      for (const line of lines) {
        if (line.resize) await desktop.evaluate(([c, r]) => (window as any).__mirror.resize(c, r), line.resize);
        if (line.d) await desktop.evaluate((b) => (window as any).__mirror.writeB64(b), line.d);
      }
      await desktop.evaluate(() => (window as any).__mirror.idle());
      await shot(desktop, "owner-terminal-while-pty-phone-sized.png");
      await desktop.context().close();
      log(`phone viewer PTY size ${phoneSize.c}x${phoneSize.r}`);
      phoneSizeSeen = phoneSize;
      // Put the PTY back to the owner's size before the run ends.
      await phonePage!.evaluate(([c, r]) => (window as any).__mirror.requestSize(c, r), [AFTER_RESIZE.cols, AFTER_RESIZE.rows]);
      await sleep(1500);
      await phonePage!.context().close();
    }
  }
  let phonePage: Page | null = null;
  let phoneSizeSeen: { c: number; r: number } | null = null;

  // Drive the run from the stand-in's event file.
  const deadline = Date.now() + 240_000;
  while (!exited && Date.now() < deadline) {
    const lines = readFileSync(events, "utf8").split("\n").filter(Boolean);
    for (; seenEvents < lines.length; seenEvents += 1) {
      const e = JSON.parse(lines[seenEvents]);
      if (e.kind === "checkpoint" && !handled.has(e.name)) {
        handled.add(e.name);
        await onCheckpoint(e.name);
      }
      if (e.kind === "exit") exited = true;
    }
    await sleep(40);
  }
  if (!exited) throw new Error("stand-in did not finish in time");
  await waitFor("owner terminal to close", () => owner.exitCode !== null, 15_000);
  await sleep(1500);
  await advanceReference(record);

  // ---- end of run: the browser's terminal text must equal the reference --------
  if (browser) {
    for (const v of viewers) {
      await compare("end-of-run", v.name, v.page, "equal");
    }
    const vt = existsSync(vtDump) ? readFileSync(vtDump, "utf8").replace(/\s+$/, "") : null;
    const ref = rowsOf(reference).join("\n").replace(/\s+$/, "");
    checks.push({ checkpoint: "end-of-run", viewer: "engine-vt100-emulator (independent implementation)", expect: "equal", equal: vt === ref, cols: 0, rows: 0, refCols: reference.cols, refRows: reference.rows, firstDiff: vt === ref ? undefined : [`vt100: ${JSON.stringify(vt?.slice(-160))}`, `ref: ${JSON.stringify(ref.slice(-160))}`] });
    log(`${vt === ref ? "EQUAL " : "DIFFER"} end-of-run / engine vt100 text vs reference`);
  }

  // ---- numbers ------------------------------------------------------------------
  const ownerRecord = readRecord(record);
  const evts = readFileSync(events, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l));
  const phases = evts.filter((e) => e.kind === "phase");
  const counts: Record<string, number> = {};
  for (const e of evts.filter((x) => x.kind === "count")) counts[e.name.split(":")[0]] = Number(e.name.split(":")[1]);
  const endT = evts.find((e) => e.kind === "exit")!.t;
  const bytesPerPhase = phases.map((p) => {
    // A phase ends at the next phase or checkpoint (the checkpoint pause is not part of it).
    const to = Math.min(endT, ...evts.filter((e) => (e.kind === "phase" || e.kind === "checkpoint") && e.t > p.t).map((e) => e.t));
    const chunks = ownerRecord.filter((l) => l.d && l.t >= p.t && l.t < to);
    const bytes = chunks.reduce((n, l) => n + Buffer.from(l.d!, "base64").length, 0);
    const seconds = (to - p.t) / 1e6;
    return { phase: p.name, seconds: Math.round(seconds * 10) / 10, bytes, chunks: chunks.length, bytesPerSecond: Math.round(bytes / seconds), updates: counts[p.name] ?? null };
  });

  // Marker joins (stand-in emit time, the owner terminal's receipt, the browser's stages).
  const emitted = new Map<number, { t: number; phase: string }>();
  if (!noMarkers && existsSync(markersFile)) {
    for (const l of readFileSync(markersFile, "utf8").split("\n").filter(Boolean)) {
      const m = JSON.parse(l);
      emitted.set(m.seq, { t: m.t, phase: m.phase });
    }
  }
  // When the owner's terminal received each marker.
  const ownerSeen = new Map<number, number>();
  {
    let tail = "";
    for (const l of ownerRecord) {
      if (!l.d) continue;
      const text = tail + Buffer.from(l.d, "base64").toString("latin1");
      for (const m of text.matchAll(/\[M(\d{6}) (\d{16})\]/g)) if (!ownerSeen.has(Number(m[1]))) ownerSeen.set(Number(m[1]), l.t);
      tail = text.slice(-40);
    }
  }
  const toOwner = [...ownerSeen].filter(([s]) => emitted.has(s)).map(([s, t]) => t - emitted.get(s)!.t);

  let latency: Record<string, unknown> | null = null;
  if (main) {
    const m: any[] = await main.evaluate(() => (window as any).__mirror.markers);
    const live = m.filter((x) => !x.viaSnapshot && emitted.has(x.seq));
    const painted = live.filter((x) => x.paintUs !== null);
    const byPhase: Record<string, any> = {};
    for (const phase of new Set(painted.map((x) => emitted.get(x.seq)!.phase))) {
      const rows = painted.filter((x) => emitted.get(x.seq)!.phase === phase);
      byPhase[phase] = dist(rows.map((x) => x.paintUs - emitted.get(x.seq)!.t));
    }
    latency = {
      method:
        "marker text [Mnnnnnn <epoch us>] is stamped by the stand-in just before its write to the PTY; the browser finds it in the frame, then in the DOM (MutationObserver) and takes the first task after that. Same machine, same wall clock, loopback. Milliseconds.",
      markersEmitted: emitted.size,
      markersSeenInBrowser: live.length,
      markersPaintedInDom: painted.length,
      markersNeverPaintedBeforeOverwritten: live.length - painted.length,
      total_emit_to_paint_ms: dist(painted.map((x) => x.paintUs - x.emitUs)),
      byPhase,
      stages_ms: {
        emit_to_relay_read: dist(painted.map((x) => x.readUs - x.emitUs)),
        relay_read_to_browser_recv: dist(painted.map((x) => x.recvUs - x.readUs)),
        recv_to_xterm_parsed: dist(painted.map((x) => x.parsedUs - x.recvUs)),
        parsed_to_dom_updated: dist(painted.map((x) => x.domUs - x.parsedUs)),
        dom_to_next_task: dist(painted.map((x) => x.paintUs - x.domUs)),
      },
    };
  }

  const results = {
    command: "bun scripts/qa/terminal_mirror_spike/run.ts" + args.map((a) => " " + a).join(""),
    mode: baseline ? "baseline (relay without tap, no host, no browser)" : noMarkers ? "byte-rate (no markers)" : "full",
    owner_terminal: `${OWNER.cols}x${OWNER.rows} then ${AFTER_RESIZE.cols}x${AFTER_RESIZE.rows}`,
    bytesPerPhase,
    stand_in_to_owner_terminal_ms: dist(toOwner),
    latency,
    checks,
    phoneSizeSeen,
    screenshots: shots,
  };
  const name = baseline ? "results-baseline.json" : noMarkers ? "results-bytes.json" : "results.json";
  writeFileSync(path.join(outDir, name), JSON.stringify(results, null, 2) + "\n");
  console.log(JSON.stringify(results, null, 2));

  await browser?.close();
  const failed = checks.filter((c) => c.expect === "equal" && !c.equal);
  if (failed.length) {
    console.error(`FAIL: ${failed.length} comparison(s) differ`, failed.map((c) => `${c.checkpoint}/${c.viewer}`));
    process.exitCode = 1;
  } else {
    console.log(`PASS: ${checks.filter((c) => c.expect === "equal").length} comparisons equal`);
  }
}

function execOut(cmd: string, a: string[]): string {
  const { spawnSync } = require("node:child_process");
  const r = spawnSync(cmd, a, { cwd: repo, encoding: "utf8" });
  return r.stdout;
}

main()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(async () => {
    for (const c of children) killTree(c);
    await sleep(800);
    for (const c of children) if (c.exitCode === null && c.pid) try { process.kill(-c.pid, "SIGKILL"); } catch {}
    if (!flag("--keep-scratch")) rmSync(scratch, { recursive: true, force: true });
    process.exit(process.exitCode ?? 0);
  });
