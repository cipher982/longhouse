#!/usr/bin/env bun
/**
 * Record the hearth reel (hearth-reel.html) to an MP4 on a virtual clock.
 *
 * The page is the real timeline over the scripted mock sessions in
 * src/dev/hearth-reel/scene.ts. Here, time only advances when we say so:
 * performance.now, Date.now and requestAnimationFrame are replaced before any
 * page script runs, and each output frame is stepped (at the sim's 60 Hz)
 * and then screenshotted. No dropped frames and no dependence on how fast
 * the machine renders.
 *
 *   bun run record:hearth-reel [--out FILE.mp4] [--fps 60|30] [--seconds N]
 *                              [--frame shelf|page] [--size 1920x1080]
 *                              [--width 960] [--height 540] [--crf 17]
 *
 * --width/--height are the page's CSS viewport. --frame shelf (default) zooms
 * onto the "Live now" rows, from the provider glyphs to the mode chips;
 * --frame page records the whole viewport. Either way the frame is rendered
 * at --size pixels, not upscaled. A poster PNG is written next to the video.
 * Needs ffmpeg on PATH.
 */

import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { parseArgs } from "node:util";
import { chromium } from "playwright";
import { createServer } from "vite";

const webDir = path.resolve(import.meta.dirname, "..");
const { values: args } = parseArgs({
  options: {
    out: { type: "string", default: path.resolve(webDir, "../video/out/hearth-reel.mp4") },
    fps: { type: "string", default: "60" },
    seconds: { type: "string" },
    width: { type: "string", default: "960" },
    height: { type: "string", default: "540" },
    frame: { type: "string", default: "shelf" },
    size: { type: "string", default: "1920x1080" },
    crf: { type: "string", default: "17" },
  },
});
const SIM_HZ = 60;
const fps = Number(args.fps);
if (SIM_HZ % fps !== 0) throw new Error(`--fps must divide ${SIM_HZ}`);
const out = path.resolve(args.out);
const poster = out.replace(/\.[^.]+$/, "") + "-poster.png";
mkdirSync(path.dirname(out), { recursive: true });

// Installed before any page script. Real time flows (rAF pumped by the real
// rAF) until freeze(); after that only step() moves the clock and runs frames.
function installVirtualClock() {
  const realRaf = window.requestAnimationFrame.bind(window);
  const realNow = performance.now.bind(performance);
  const realDateNow = Date.now;
  let frozen = false;
  let now = 0;
  let wallBase = 0;
  let queue = new Map();
  let nextId = 1;
  performance.now = () => (frozen ? now : realNow());
  Date.now = () => (frozen ? wallBase + now : realDateNow());
  window.requestAnimationFrame = (cb) => {
    queue.set(nextId, cb);
    return nextId++;
  };
  window.cancelAnimationFrame = (id) => queue.delete(id);
  const flush = (ts) => {
    const due = queue;
    queue = new Map();
    for (const cb of due.values()) cb(ts);
  };
  const pump = () => {
    if (frozen) return;
    flush(realNow());
    realRaf(pump);
  };
  realRaf(pump);
  // Let React commit and run effects (MessageChannel tasks) between frames.
  const settle = () => new Promise((r) => setTimeout(r, 4));
  window.__vclock = {
    freeze() {
      now = realNow();
      wallBase = realDateNow() - now;
      frozen = true;
    },
    async step(ms, frames) {
      for (let i = 0; i < frames; i++) {
        now += ms;
        flush(now);
        await settle();
      }
    },
  };
}

const started = Date.now();
const server = await createServer({
  root: webDir,
  configFile: path.join(webDir, "vite.config.ts"),
  logLevel: "error",
  server: { host: "127.0.0.1", port: 0, watch: null },
});
await server.listen();
const url = `${server.resolvedUrls.local[0]}hearth-reel.html?record`;

const browser = await chromium.launch({ args: ["--ignore-gpu-blocklist", "--enable-gpu", "--use-angle=metal"] });
let ffmpeg;
try {
  const width = Number(args.width);
  const height = Number(args.height);
  const [outW, outH] = args.size.split("x").map(Number);
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 2 });
  page.on("pageerror", (e) => console.error("page error:", e.message));
  await page.addInitScript(installVirtualClock);
  await page.goto(url);
  await page.waitForFunction(() => window.__hearthReel && window.__longhouseHearth?.(), null, { timeout: 30_000 }).catch(() => {
    throw new Error("the WebGL fire never started (no WebGL2 float targets in this browser?)");
  });
  await page.evaluate(() => document.fonts.ready);
  const { warmup, duration } = await page.evaluate(() => {
    window.__vclock.freeze();
    window.__hearthReel.restart();
    return { warmup: window.__hearthReel.warmup, duration: window.__hearthReel.duration };
  });
  // The camera, in CSS px at the output aspect. The page is re-rendered at the
  // pixel density that makes the camera exactly --size, so zoom stays crisp.
  const camera = await page.evaluate(([frame, aspect]) => {
    if (frame === "page") return { x: 0, y: 0, width: innerWidth, height: innerHeight };
    const shelf = document.querySelector(".inbox-tier--shelf")?.getBoundingClientRect();
    if (!shelf) throw new Error("no Live now shelf on the page");
    // Right edge: halfway between the mode chips and the "on <machine>" column.
    const rects = (sel) => [...document.querySelectorAll(`.inbox-tier--shelf ${sel}`)].map((e) => e.getBoundingClientRect());
    const chipRight = Math.max(...rects(".inbox-row-mode-chip").map((r) => r.right));
    const machineLeft = Math.min(...rects(".inbox-row-machine").map((r) => r.left));
    const x = Math.floor(Math.max(0, shelf.left - 14));
    // Whole CSS px in the output aspect, so the capture is exactly --size pixels.
    const unit = 16;
    const w = Math.floor((Math.min(innerWidth, (chipRight + machineLeft) / 2) - x) / unit) * unit;
    const h = Math.round(w / aspect);
    // Centred on the shelf, but never showing the toolbar's bottom edge above it.
    // History sits too close below to fit whole, so the shelf shot hides it.
    const toolbar = document.querySelector(".sessions-toolbar")?.getBoundingClientRect().bottom ?? 0;
    const y = Math.floor(Math.max(toolbar + 12, shelf.top + shelf.height / 2 - h / 2));
    const history = document.querySelector(".inbox-tier--history");
    if (history && history.getBoundingClientRect().top < y + h) history.style.visibility = "hidden";
    return { x, y, width: w, height: h };
  }, [args.frame, outW / outH]);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: outW / camera.width, mobile: false });
  const seconds = args.seconds ? Number(args.seconds) : duration;
  const stepMs = 1000 / SIM_HZ + 1e-6; // a hair over H so each frame advances exactly one sim step
  const perFrame = SIM_HZ / fps;
  await page.evaluate(([ms, n]) => window.__vclock.step(ms, n), [stepMs, Math.round(warmup * SIM_HZ)]);

  ffmpeg = spawn(
    "ffmpeg",
    ["-v", "error", "-y", "-f", "image2pipe", "-framerate", String(fps), "-c:v", "png", "-i", "-", "-an",
      "-vf", `scale=${outW}:${outH}:flags=lanczos`,
      "-c:v", "libx264", "-preset", "slow", "-crf", args.crf, "-profile:v", "high", "-pix_fmt", "yuv420p",
      "-movflags", "+faststart", out],
    { stdio: ["pipe", "inherit", "inherit"] },
  );
  const encoded = new Promise((resolve, reject) => {
    ffmpeg.on("error", reject);
    ffmpeg.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`ffmpeg exited ${code}`))));
  });
  const total = Math.round(seconds * fps);
  const posterAt = Math.round(total * 0.4);
  for (let i = 0; i < total; i++) {
    await page.evaluate(([ms, n]) => window.__vclock.step(ms, n), [stepMs, perFrame]);
    const { data } = await cdp.send("Page.captureScreenshot", { format: "png", clip: { ...camera, scale: 1 } });
    const png = Buffer.from(data, "base64");
    if (i === posterAt) writeFileSync(poster, png);
    if (!ffmpeg.stdin.write(png)) await new Promise((r) => ffmpeg.stdin.once("drain", r));
    if (i % fps === 0) process.stdout.write(`\rframe ${i}/${total}`);
  }
  ffmpeg.stdin.end();
  await encoded;
  console.log(`\r${out} (${total} frames at ${fps} fps, ${outW}x${outH}) in ${((Date.now() - started) / 1000).toFixed(1)} s`);
  console.log(poster);
} finally {
  ffmpeg?.stdin.destroyed === false && ffmpeg.stdin.end();
  await browser.close();
  await server.close();
}
