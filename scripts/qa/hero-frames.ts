#!/usr/bin/env bun
/**
 * Hero demo frame harness: what a visitor sees, second by second, without
 * anyone taking screenshots by hand.
 *
 * The landing hero is DOM driven by a clock, not a video, so there is no file
 * to look at. This steps the frozen clock (`?demoT=`) through one loop at
 * each viewport and writes, per run:
 *
 *   <viewport>/t<sec>.png      the demo element at that instant
 *   <viewport>/fold.png        the full first viewport, hero scrolled to top
 *   <viewport>-sheet.png       labelled contact sheet of every frame
 *   frames.json                per frame: chapter, caption, and each visible
 *                              stage layer's opacity, box, and text — the
 *                              same frames for agents that cannot read images
 *
 * Usage:
 *   bun scripts/qa/hero-frames.ts [--step=0.5] [--from=S --to=S] [--viewport=desktop|mobile] [--output=DIR]
 *
 * Narrow --from/--to with a small --step to inspect one transition.
 *
 * Starts Vite on :47210 when nothing serves FRONTEND_URL and stops it after.
 */

import { chromium } from "playwright";
import { spawn, execFileSync } from "child_process";
import { mkdirSync, writeFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { HERO_CHAPTERS, HERO_DURATION_SEC } from "../../video/src/demo/story";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const arg = (name: string) =>
  process.argv.find((a) => a.startsWith(`--${name}=`))?.split("=")[1];

const STEP = Number(arg("step") ?? "1");
const BASE_URL = process.env.FRONTEND_URL ?? "http://localhost:47210";
const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
const OUT = path.resolve(arg("output") ?? path.join(REPO_ROOT, "artifacts/hero-frames", stamp));
const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900, isMobile: false },
  { name: "mobile", width: 390, height: 844, isMobile: true },
].filter((v) => !arg("viewport") || v.name === arg("viewport"));

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function isServing(url: string): Promise<boolean> {
  try {
    return (await fetch(url, { signal: AbortSignal.timeout(1500) })).ok;
  } catch {
    return false;
  }
}

async function ensureFrontend(): Promise<() => void> {
  if (await isServing(BASE_URL)) return () => {};
  const port = new URL(BASE_URL).port;
  const child = spawn("bunx", ["vite", "--port", port, "--strictPort"], {
    cwd: path.join(REPO_ROOT, "web"),
    stdio: "ignore",
    detached: true,
  });
  const stop = () => {
    try {
      process.kill(-child.pid!, "SIGTERM");
    } catch {
      /* gone */
    }
  };
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (await isServing(BASE_URL)) return stop;
    await sleep(300);
  }
  stop();
  throw new Error(`Vite did not serve ${BASE_URL} within 60s`);
}

const times: number[] = [];
const FROM = Number(arg("from") ?? "0");
const TO = Math.min(Number(arg("to") ?? HERO_DURATION_SEC), HERO_DURATION_SEC);
for (let t = FROM; t < TO; t += STEP) times.push(Math.round(t * 100) / 100);

mkdirSync(OUT, { recursive: true });
const stopFrontend = await ensureFrontend();
const browser = await chromium.launch();
const report: Record<string, unknown> = {
  durationSec: HERO_DURATION_SEC,
  chapters: HERO_CHAPTERS,
  viewports: {},
};

try {
  for (const vp of VIEWPORTS) {
    const dir = path.join(OUT, vp.name);
    mkdirSync(dir, { recursive: true });
    const context = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      isMobile: vp.isMobile,
      hasTouch: vp.isMobile,
      deviceScaleFactor: 2,
    });
    try {
      const page = await context.newPage();
      // Only real API calls: a `**/api/**` glob also swallows Vite's src/services/api modules.
      await page.route((url) => url.pathname.startsWith("/api/"), (r) => r.abort());
      await page.goto(`${BASE_URL}/landing?demoT=1`, { waitUntil: "domcontentloaded" });
      await page.waitForFunction(() => "__heroDemoSeek" in window, null, { timeout: 30_000 });
      await document_fonts(page);

      await page.evaluate(() => document.querySelector(".landing-hero")?.scrollIntoView());
      await page.screenshot({ path: path.join(dir, "fold.png") });

      const demo = page.locator(".landing-hero .hero-demo");
      const frames = [];
      for (const t of times) {
        await page.evaluate((sec) => (window as any).__heroDemoSeek(sec), t);
        await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
        const file = `t${t.toFixed(2).padStart(5, "0")}.png`;
        await demo.screenshot({ path: path.join(dir, file) });
        const state = await page.evaluate(() => {
          const demoEl = document.querySelector(".landing-hero .hero-demo")!;
          const origin = demoEl.getBoundingClientRect();
          const box = (el: Element) => {
            const r = el.getBoundingClientRect();
            return [r.x - origin.x, r.y - origin.y, r.width, r.height].map(Math.round);
          };
          const layers = [...demoEl.querySelectorAll<HTMLElement>(".hero-stage-item")]
            .map((el) => ({ el, opacity: Number(getComputedStyle(el).opacity) }))
            .filter((l) => l.opacity > 0.01)
            .map(({ el, opacity }) => ({
              class: (el.getAttribute("class") ?? "").replace("hero-stage-item ", ""),
              opacity: Math.round(opacity * 100) / 100,
              box: box(el),
              text: (el.innerText ?? el.textContent ?? "").split("\n").map((l) => l.trimEnd()).filter(Boolean),
            }));
          return {
            caption: demoEl.querySelector(".hero-demo-caption")?.textContent ?? "",
            demoBox: [origin.x, origin.y, origin.width, origin.height].map(Math.round),
            chapter: (demoEl as HTMLElement).dataset.heroChapter ?? "",
            layers,
          };
        });
        frames.push({ t, file, ...state });
      }
      (report.viewports as Record<string, unknown>)[vp.name] = frames;

      execFileSync("magick", [
        "montage",
        ...times.map((t) => [
          "-label",
          `t=${t.toFixed(1)}s`,
          path.join(dir, `t${t.toFixed(2).padStart(5, "0")}.png`),
        ]).flat(),
        "-tile", vp.isMobile ? "6x" : "4x",
        "-geometry", vp.isMobile ? "260x+8+8" : "480x+8+8",
        "-background", "#222",
        "-fill", "#eee",
        "-font", "/System/Library/Fonts/Supplemental/Arial.ttf",
        "-pointsize", "22",
        path.join(OUT, `${vp.name}-sheet.png`),
      ]);
    } finally {
      await context.close();
    }
  }
  writeFileSync(path.join(OUT, "frames.json"), JSON.stringify(report, null, 2));
  console.log(`hero frames: ${OUT} (${times.length} frames x ${VIEWPORTS.length} viewports; chapters ${HERO_CHAPTERS.map((c) => c.id).join(",")})`);
} finally {
  await browser.close();
  stopFrontend();
}

async function document_fonts(page: import("playwright").Page) {
  await page.evaluate(() => document.fonts.ready.then(() => true));
}
