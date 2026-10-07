#!/usr/bin/env bun
/**
 * UI gallery: every fixture-backed page/scene at desktop, wide and phone
 * sizes, on one HTML contact sheet.
 *
 * One Vite serves the whole sweep; each capture is its own ui-capture.ts
 * process (its own Chromium), JOBS at a time. Fixture scenes answer every API
 * call from Playwright routes, so nothing else needs to be running.
 *
 * The newest iOS renders already downloaded under artifacts/ (ios-previews,
 * ios-ui-shot, and sim-shot's artifacts/sim) are linked at the end. Nothing is
 * dispatched.
 *
 * --sweep also opens every menu on every frame and fails the run if one
 * lands off-screen, covered or clipped (scripts/ui/popover-sweep.ts); that is
 * what `make ui-sweep` and CI run.
 *
 * Usage:
 *   bunx tsx scripts/ui/ui-gallery.ts [--jobs=4] [--only=session] [--viewports=desktop,wide,mobile] [--output=DIR] [--sweep]
 */

import { execSync, spawn, type ChildProcess } from "child_process";
import { createWriteStream, existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "fs";
import os from "os";
import path from "path";
import { ensureFrontend, REPO_ROOT } from "./frontend";

const VIEWPORTS = {
  desktop: "1440x900",
  wide: "2000x1200",
  mobile: "mobile",
} as const;
type ViewportKey = keyof typeof VIEWPORTS;

type Job = { page: string; scene: string; variant?: string };

// Every page/scene pair ui-capture.ts can render from fixtures alone.
// Settings, profile and integrations have no fixture scene and are left out.
const JOBS: Job[] = [
  { page: "session-detail", scene: "session-prose-idle" },
  { page: "session-detail", scene: "session-prose-idle", variant: "terminal" },
  { page: "session-detail", scene: "session-console" },
  { page: "session-detail", scene: "session-unrecorded-inputs" },
  { page: "session-detail", scene: "session-detail-stress" },
  { page: "session-detail", scene: "session-detail-stress", variant: "terminal" },
  { page: "session-detail", scene: "session-tones" },
  { page: "session-detail", scene: "session-question" },
  { page: "session-detail", scene: "session-attention" },
  { page: "session-detail", scene: "session-stale-observation" },
  { page: "session-detail", scene: "session-resume" },
  { page: "session-detail", scene: "session-ended" },
  { page: "session-detail", scene: "session-input-outbox" },
  { page: "session-detail", scene: "session-remote-image-outbox" },
  { page: "session-detail", scene: "session-background-notices" },
  { page: "session-detail", scene: "landing-session" },
  { page: "timeline", scene: "timeline-card-stress" },
  { page: "timeline", scene: "timeline-hearth" },
  { page: "timeline", scene: "first-run" },
  { page: "timeline", scene: "first-run-machine" },
  { page: "timeline", scene: "launch-model-picker" },
  { page: "timeline", scene: "launch-model-picked" },
  { page: "new-session", scene: "new-session" },
  { page: "timeline", scene: "launch-unavailable" },
  { page: "timeline", scene: "launch-no-machines" },
  { page: "timeline", scene: "landing" },
  { page: "timeline", scene: "landing-search" },
  { page: "machines", scene: "machines-fleet" },
  { page: "machines", scene: "machines-unavailable" },
  { page: "machines", scene: "first-run" },
  { page: "machines", scene: "first-run-machine" },
  { page: "machine-detail", scene: "machines-fleet" },
  { page: "machine-detail", scene: "first-run-machine" },
  { page: "devices", scene: "devices-revoke" },
  { page: "devices", scene: "devices-list" },
  { page: "login", scene: "login" },
  { page: "landing", scene: "provider-certification" },
  { page: "security", scene: "first-run" },
  { page: "privacy", scene: "first-run" },
];

type Frame = { name: string; file: string };
type Capture = { job: Job; viewport: ViewportKey; frames: Frame[]; error?: string; seconds: number };

function arg(name: string): string | undefined {
  return process.argv.find((a) => a.startsWith(`--${name}=`))?.slice(name.length + 3);
}

function jobKey(job: Job): string {
  return [job.page, job.scene, job.variant].filter(Boolean).join("--");
}

const SWEEP = process.argv.includes("--sweep");

const children = new Set<ChildProcess>();
// Set on Ctrl-C: no new capture starts once the sweep is stopping.
let stopping = false;

/**
 * SIGKILL what a finished capture left in its process group (a Chromium whose
 * leader died without closing it). Probe first: a group ID stays reserved
 * while any member lives, so only a live group is signalled, never a reused id.
 */
function reapGroup(pgid: number): void {
  try {
    process.kill(-pgid, 0);
  } catch {
    return; // group already empty
  }
  try {
    process.kill(-pgid, "SIGKILL");
  } catch {
    /* emptied in between */
  }
}

function runCapture(job: Job, viewport: ViewportKey, outDir: string, frontendUrl: string): Promise<Capture> {
  const dir = path.join(outDir, jobKey(job), viewport);
  mkdirSync(dir, { recursive: true });
  // Under bun (the CI lane, whose guest has no network to fetch tsx) bun runs
  // the capture itself; under tsx, tsx does.
  const [command, ...prefix] = process.versions.bun ? ["bun"] : ["bunx", "tsx"];
  const args = [
    ...prefix,
    "scripts/ui/ui-capture.ts",
    job.page,
    `--scene=${job.scene}`,
    `--viewport=${VIEWPORTS[viewport]}`,
    `--output=${dir}`,
    "--no-trace",
    ...(job.variant ? [`--css-variant=${job.variant}`] : []),
    ...(SWEEP ? ["--sweep"] : []),
  ];
  const started = Date.now();
  return new Promise((resolve) => {
    const log = createWriteStream(path.join(dir, "capture.log"));
    const child = spawn(command, args, {
      cwd: REPO_ROOT,
      env: { ...process.env, FRONTEND_URL: frontendUrl },
      stdio: ["ignore", "pipe", "pipe"],
      // Its own process group, so stopping it also stops its Chromium.
      detached: true,
    });
    children.add(child);
    child.stdout?.pipe(log);
    child.stderr?.pipe(log);
    child.on("close", (code) => {
      children.delete(child);
      reapGroup(child.pid!);
      const seconds = (Date.now() - started) / 1000;
      const frames: Frame[] = [];
      let error = code === 0 ? undefined : `exit ${code}`;
      const manifestPath = path.join(dir, "manifest.json");
      if (existsSync(manifestPath)) {
        const manifest = JSON.parse(readFileSync(manifestPath, "utf-8"));
        for (const [name, artifact] of Object.entries(manifest.artifacts ?? {})) {
          const shot = (artifact as { screenshotPath?: string } | null)?.screenshotPath;
          if (shot) frames.push({ name, file: path.relative(outDir, path.resolve(REPO_ROOT, shot)) });
        }
        if (manifest.errors?.length) error = manifest.errors.join("; ");
      }
      resolve({ job, viewport, frames, error, seconds });
    });
  });
}

async function runPool<T>(tasks: Array<() => Promise<T>>, limit: number): Promise<T[]> {
  const results: T[] = new Array(tasks.length);
  let next = 0;
  const worker = async () => {
    while (next < tasks.length && !stopping) {
      const index = next++;
      results[index] = await tasks[index]();
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, worker));
  return results;
}

/** The primary checkout's artifacts/ too: dispatched iOS evidence lands there, not in worktrees. */
function artifactRoots(): string[] {
  const roots = [path.join(REPO_ROOT, "artifacts")];
  try {
    const common = execSync("git rev-parse --path-format=absolute --git-common-dir", { cwd: REPO_ROOT, encoding: "utf-8" }).trim();
    roots.push(path.join(path.dirname(common), "artifacts"));
  } catch {
    /* not a git checkout */
  }
  return [...new Set(roots)].filter((root) => existsSync(root));
}

type IosSet = { kind: string; dir: string; mtime: Date; files: string[] };

/** The newest directory of PNGs per iOS render kind, by newest file in it. */
function newestIosSets(): IosSet[] {
  const kinds = ["ios-previews", "ios-ui-shot", "sim"];
  const byDir = new Map<string, { kind: string; mtime: number; files: string[] }>();
  const walk = (dir: string, depth: number) => {
    if (depth > 10) return;
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full, depth + 1);
      } else if (entry.name.endsWith(".png")) {
        const kind = kinds.find((k) => full.split(path.sep).includes(k));
        if (!kind) continue;
        const parent = path.dirname(full);
        const mtime = statSync(full).mtimeMs;
        const set = byDir.get(parent) ?? { kind, mtime: 0, files: [] };
        set.mtime = Math.max(set.mtime, mtime);
        set.files.push(full);
        byDir.set(parent, set);
      }
    }
  };
  for (const root of artifactRoots()) walk(root, 0);
  const newest = new Map<string, IosSet>();
  for (const [dir, set] of byDir) {
    const current = newest.get(set.kind);
    if (!current || set.mtime > current.mtime.getTime()) {
      newest.set(set.kind, { kind: set.kind, dir, mtime: new Date(set.mtime), files: set.files.sort() });
    }
  }
  return [...newest.values()];
}

const escapeHtml = (value: string) =>
  value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function renderIndex(outDir: string, captures: Capture[], iosSets: IosSet[], meta: string): string {
  const groups = new Map<string, Capture[]>();
  for (const capture of captures) {
    const key = jobKey(capture.job);
    groups.set(key, [...(groups.get(key) ?? []), capture]);
  }
  const viewportKeys = Object.keys(VIEWPORTS) as ViewportKey[];
  const sections = [...groups.entries()].map(([key, list]) => {
    const { job } = list[0];
    const cells = viewportKeys.map((viewport) => {
      const capture = list.find((c) => c.viewport === viewport);
      const shots = (capture?.frames ?? [])
        .map(
          (frame) =>
            `<figure><img loading="lazy" src="${escapeHtml(frame.file)}" alt="${escapeHtml(`${key} ${viewport} ${frame.name}`)}">` +
            (capture!.frames.length > 1 ? `<figcaption>${escapeHtml(frame.name)}</figcaption>` : "") +
            `</figure>`,
        )
        .join("");
      const error = capture?.error ? `<p class="error">${escapeHtml(capture.error)}</p>` : "";
      return `<div class="cell cell--${viewport}"><h3>${viewport} <span>${VIEWPORTS[viewport]}</span></h3>${error}${shots}</div>`;
    });
    const variant = job.variant ? ` <em>css variant: ${escapeHtml(job.variant)}</em>` : "";
    return `<section id="${escapeHtml(key)}"><h2>${escapeHtml(job.page)} · ${escapeHtml(job.scene)}${variant}</h2><div class="row">${cells.join("")}</div></section>`;
  });
  const ios = iosSets.length
    ? iosSets
        .map(
          (set) =>
            `<section><h2>iOS · ${escapeHtml(set.kind)} <em>${escapeHtml(set.mtime.toISOString())} · ${escapeHtml(set.dir)}</em></h2><div class="ios">${set.files
              .map((file) => `<figure><img loading="lazy" src="file://${escapeHtml(file)}" alt="${escapeHtml(path.basename(file))}"><figcaption>${escapeHtml(path.basename(file))}</figcaption></figure>`)
              .join("")}</div></section>`,
        )
        .join("")
    : `<section><h2>iOS</h2><p>No downloaded iOS renders under artifacts/. Run <code>make ios-previews</code> (hosted macOS VM) or <code>make sim-shot</code>.</p></section>`;
  const toc = [...groups.keys()].map((key) => `<a href="#${escapeHtml(key)}">${escapeHtml(key)}</a>`).join("");
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Longhouse UI gallery</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 16px 24px 48px; background: #12100d; color: #eadfcb; font: 14px/1.4 -apple-system, system-ui, sans-serif; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .meta { color: #9e8f78; margin: 0 0 12px; }
  nav { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-bottom: 24px; font-size: 12px; }
  nav a, a { color: #d4b87a; }
  section { margin: 0 0 32px; }
  h2 { font-size: 15px; margin: 0 0 8px; position: sticky; top: 0; background: #12100d; padding: 6px 0; z-index: 1; }
  h2 em { font-weight: normal; color: #cc9054; font-size: 13px; }
  h3 { font-size: 12px; margin: 0 0 6px; color: #9e8f78; font-weight: 600; }
  h3 span { font-weight: normal; }
  .row { display: grid; grid-template-columns: 1fr 1.35fr 0.3fr; gap: 16px; align-items: start; }
  .cell figure, .ios figure { margin: 0 0 8px; }
  img { width: 100%; display: block; border: 1px solid #3a2f24; cursor: zoom-in; background: #000; }
  figcaption { font-size: 11px; color: #9e8f78; margin-top: 2px; }
  .ios { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; }
  .error { color: #f08a5d; font-size: 12px; white-space: pre-wrap; }
  dialog { padding: 0; border: 0; background: transparent; max-width: 98vw; max-height: 98vh; }
  dialog::backdrop { background: rgba(0, 0, 0, 0.85); }
  dialog img { width: auto; max-width: 98vw; max-height: 96vh; cursor: zoom-out; }
  @media (max-width: 900px) { .row { grid-template-columns: 1fr; } }
</style></head>
<body>
<h1>Longhouse UI gallery</h1>
<p class="meta">${escapeHtml(meta)}</p>
<nav>${toc}<a href="#ios">iOS</a></nav>
${sections.join("\n")}
<div id="ios">${ios}</div>
<dialog id="zoom"><img alt=""></dialog>
<script>
  const zoom = document.getElementById("zoom");
  const zoomImg = zoom.querySelector("img");
  document.addEventListener("click", (event) => {
    if (event.target === zoom) { zoom.close(); return; }
    const img = event.target.closest("img");
    if (!img) return;
    if (zoom.open) { zoom.close(); return; }
    zoomImg.src = img.src;
    zoom.showModal();
  });
</script>
</body></html>
`;
}

async function main() {
  const started = Date.now();
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
  const outDir = path.resolve(arg("output") ?? path.join("/tmp/agents/ui-gallery", stamp));
  const jobsLimit = Number(arg("jobs") ?? Math.max(1, Math.min(4, Math.floor(os.cpus().length / 2))));
  if (!Number.isInteger(jobsLimit) || jobsLimit < 1) {
    throw new Error(`--jobs must be a positive integer, got ${arg("jobs")}`);
  }
  const only = arg("only");
  const frontendUrl = process.env.GALLERY_FRONTEND_URL ?? "http://localhost:47291";
  mkdirSync(outDir, { recursive: true });

  const jobs = JOBS.filter((job) => !only || jobKey(job).includes(only));
  // --viewports=desktop,mobile shards the run (CI runs one viewport per job).
  const viewportArg = arg("viewports");
  const viewportKeys = (viewportArg ? viewportArg.split(",") : Object.keys(VIEWPORTS)) as ViewportKey[];
  for (const key of viewportKeys) {
    if (!(key in VIEWPORTS)) throw new Error(`--viewports takes ${Object.keys(VIEWPORTS).join(", ")}; got ${key}`);
  }
  const tasks = jobs.flatMap((job) => viewportKeys.map((viewport) => ({ job, viewport })));
  console.log(`UI gallery: ${tasks.length} captures (${jobs.length} scenes x ${viewportKeys.length} viewports), ${jobsLimit} at a time`);
  console.log(`Output: ${outDir}`);

  const stopFrontend = await ensureFrontend(frontendUrl, { handleSignals: false });
  const signalChildren = (signal: NodeJS.Signals) => {
    for (const child of children) {
      try {
        process.kill(-child.pid!, signal);
      } catch {
        /* already gone */
      }
    }
  };
  // SIGTERM each capture's process group, then SIGKILL whatever is left.
  const killChildren = async () => {
    stopping = true;
    signalChildren("SIGTERM");
    const deadline = Date.now() + 3000;
    while (children.size > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    signalChildren("SIGKILL");
  };
  // One shutdown, whether Ctrl-C or the sweep finishing gets there first.
  let shutdown: Promise<void> | null = null;
  const shutdownOnce = () => (shutdown ??= killChildren().then(stopFrontend));
  const onSignal = () => {
    void shutdownOnce().finally(() => process.exit(130));
  };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);

  let captures: Capture[] = [];
  try {
    let done = 0;
    // A stopped sweep leaves holes for the captures it never started.
    captures = (await runPool(
      tasks.map(({ job, viewport }) => async () => {
        const capture = await runCapture(job, viewport, outDir, frontendUrl);
        done += 1;
        const status = capture.error ? `FAILED (${capture.error})` : `${capture.frames.length} frame(s)`;
        console.log(`  [${done}/${tasks.length}] ${jobKey(job)} ${viewport} ${capture.seconds.toFixed(1)}s ${status}`);
        return capture;
      }),
      jobsLimit,
    )).filter(Boolean);
  } finally {
    await shutdownOnce();
  }

  const iosSets = newestIosSets();
  const elapsed = ((Date.now() - started) / 1000).toFixed(1);
  let commit = "unknown";
  try {
    commit = execSync("git rev-parse --short HEAD", { cwd: REPO_ROOT, encoding: "utf-8" }).trim();
  } catch {
    /* not a git checkout */
  }
  const meta = `${new Date().toISOString()} · ${commit} · ${captures.length} captures in ${elapsed}s`;
  const indexPath = path.join(outDir, "index.html");
  writeFileSync(indexPath, renderIndex(outDir, captures, iosSets, meta));

  if (SWEEP) {
    let opened = 0;
    const lines: string[] = [];
    for (const capture of captures) {
      const dir = path.join(outDir, jobKey(capture.job), capture.viewport);
      for (const file of existsSync(dir) ? readdirSync(dir).filter((name) => name.endsWith("-popovers.json")) : []) {
        const report = JSON.parse(readFileSync(path.join(dir, file), "utf-8"));
        opened += report.overlaysChecked;
        for (const failure of report.failures) {
          lines.push(`  ${jobKey(capture.job)} ${capture.viewport}: ${failure.rule} ${failure.overlay} from ${failure.trigger}: ${failure.detail}`);
        }
      }
    }
    console.log(`\nPopover sweep: ${opened} overlays opened, ${lines.length} failing`);
    for (const line of lines) console.log(line);
  }

  const failed = captures.filter((capture) => capture.error);
  for (const capture of failed) {
    console.error(`FAILED ${jobKey(capture.job)} ${capture.viewport}: ${capture.error} (log: ${path.join(outDir, jobKey(capture.job), capture.viewport, "capture.log")})`);
  }
  console.log(`\niOS: ${iosSets.length ? iosSets.map((set) => `${set.kind} ${set.mtime.toISOString()}`).join(", ") : "none downloaded"}`);
  console.log(`Elapsed: ${elapsed}s`);
  console.log(`Gallery: ${indexPath}`);
  if (failed.length > 0) process.exitCode = 1;
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
