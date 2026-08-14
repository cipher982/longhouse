#!/usr/bin/env bun

import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { chromium, type Browser, type Page } from "playwright";

interface Capture {
  frame: number;
  relativePath: string;
  bytes: number;
}

interface CaptureGroup {
  name: string;
  width: number;
  height: number;
  captureMode: "stage" | "viewport";
  captures: Capture[];
  contactSheet: string;
}

interface PlaybackCheck {
  advancingFrame: number;
  loopedFrame: number;
  initialTask: string;
  loopedTask: string;
  initialCycle: number;
  loopedCycle: number;
  elapsedMs: number;
}

const args = process.argv.slice(2);
const argValue = (name: string): string | undefined =>
  args.find((arg) => arg.startsWith(`--${name}=`))?.slice(name.length + 3);

const sampleEvery = Number.parseInt(argValue("every") ?? "6", 10);
if (!Number.isInteger(sampleEvery) || sampleEvery < 1) {
  throw new Error("--every must be a positive integer");
}

const timestamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
const outputDir = path.resolve(argValue("output") ?? `artifacts/remote-scene-qa/${timestamp}`);
const baseUrl = (process.env.FRONTEND_URL ?? "http://localhost:47200").replace(/\/$/, "");
const demoSeed = argValue("seed") ?? "remote-scene-qa";
const sceneUrl = `${baseUrl}/landing?${new URLSearchParams({ demoSeed })}`;
const sceneFps = 24;

mkdirSync(outputDir, { recursive: true });

const browserErrors: string[] = [];
const failedRequests: string[] = [];

async function settleFrame(page: Page): Promise<void> {
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  await page.waitForTimeout(40);
}

async function prepareScene(page: Page, width: number, height: number): Promise<void> {
  await page.setViewportSize({ width, height });
  await page.goto(sceneUrl, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  const stage = page.locator(".remote-scene-stage");
  await stage.scrollIntoViewIfNeeded();
  await stage.waitFor({ state: "visible" });
  await page.waitForFunction(
    () => document.querySelector(".remote-scene-stage")?.getAttribute("data-scene-ready") === "true",
    undefined,
    { timeout: 10_000 },
  );
  await settleFrame(page);
}

async function captureFrames(
  page: Page,
  groupName: string,
  width: number,
  height: number,
  captureCount: number,
  captureMode: "stage" | "viewport",
  intervalFrames: number,
): Promise<Capture[]> {
  await prepareScene(page, width, height);
  const stage = page.locator(".remote-scene-stage");
  const captures: Capture[] = [];

  for (let index = 0; index < captureCount; index += 1) {
    if (index > 0) await page.waitForTimeout((intervalFrames / sceneFps) * 1000);
    const frame = Number(await stage.getAttribute("data-scene-frame"));
    const filename = `${groupName}-${String(index).padStart(2, "0")}-frame-${String(frame).padStart(3, "0")}.png`;
    const absolutePath = path.join(outputDir, filename);
    if (captureMode === "viewport") {
      await page.screenshot({ path: absolutePath, animations: "disabled" });
    } else {
      await stage.screenshot({ path: absolutePath, animations: "disabled" });
    }
    captures.push({ frame, relativePath: filename, bytes: statSync(absolutePath).size });
  }
  return captures;
}

function escapeHtml(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function imageDataUrl(capture: Capture): string {
  return `data:image/png;base64,${readFileSync(path.join(outputDir, capture.relativePath)).toString("base64")}`;
}

async function createContactSheet(
  browser: Browser,
  groupName: string,
  captures: Capture[],
  columns: number,
): Promise<string> {
  const cardWidth = groupName.startsWith("mobile") ? 330 : 500;
  const gap = 18;
  const pageWidth = columns * cardWidth + (columns + 1) * gap;
  const sheetPage = await browser.newPage({ viewport: { width: pageWidth, height: 900 } });
  const cards = captures.map((capture) => `
    <figure>
      <img src="${imageDataUrl(capture)}" alt="Frame ${capture.frame}">
      <figcaption>frame ${String(capture.frame).padStart(3, "0")}</figcaption>
    </figure>`).join("");
  await sheetPage.setContent(`<!doctype html>
    <html><head><meta charset="utf-8"><style>
      * { box-sizing: border-box; }
      body { margin: 0; padding: ${gap}px; color: #c9bba7; background: #0d0908; font: 13px ui-monospace, monospace; }
      main { display: grid; grid-template-columns: repeat(${columns}, minmax(0, 1fr)); gap: ${gap}px; }
      figure { margin: 0; overflow: hidden; background: #120b09; border: 1px solid #3d3428; border-radius: 10px; }
      img { display: block; width: 100%; height: auto; }
      figcaption { padding: 9px 11px; border-top: 1px solid #3d3428; }
    </style></head><body><main>${cards}</main></body></html>`, { waitUntil: "load" });
  const filename = `${groupName}-contact-sheet.png`;
  await sheetPage.screenshot({ path: path.join(outputDir, filename), fullPage: true });
  await sheetPage.close();
  return filename;
}

function writeReviewFiles(groups: CaptureGroup[], frameCount: number, playbackCheck: PlaybackCheck): void {
  const prompt = `Review the visual sequence in this directory as a neutral critic.

Start with the contact sheets, then inspect any individual frames that help. Judge the work using your own standards. Describe what you think is depicted and how the sequence reads before discussing what feels effective, weak, confusing, unfinished, or worth changing. Be candid and specific. Do not inspect the source code or assume an intended story beyond what the images themselves communicate. Do not use a numeric score unless it genuinely helps your judgment.
`;
  writeFileSync(path.join(outputDir, "review-prompt.md"), prompt);

  const manifest = {
    schemaVersion: 2,
    capturedAt: new Date().toISOString(),
    sourceUrl: sceneUrl,
    gitCommit: execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim(),
    frameCount,
    sampleEvery,
    groups,
    browserErrors,
    failedRequests,
    playbackCheck,
    reviewPrompt: "review-prompt.md",
  };
  writeFileSync(path.join(outputDir, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);

  const sections = groups.map((group) => `
    <section><h2>${escapeHtml(group.name)}</h2>
      <a href="${escapeHtml(group.contactSheet)}"><img src="${escapeHtml(group.contactSheet)}" alt="${escapeHtml(group.name)} contact sheet"></a>
    </section>`).join("");
  writeFileSync(path.join(outputDir, "index.html"), `<!doctype html>
    <html><head><meta charset="utf-8"><title>Remote scene visual QA</title><style>
      body { max-width: 1400px; margin: 0 auto; padding: 32px; color: #eee4d6; background: #0d0908; font-family: system-ui, sans-serif; }
      h1, h2 { font-weight: 600; } section { margin: 40px 0; } img { display: block; width: 100%; border: 1px solid #3d3428; }
    </style></head><body><h1>Remote scene visual QA</h1>${sections}</body></html>\n`);
}

async function verifyLivePlayback(page: Page): Promise<PlaybackCheck> {
  await prepareScene(page, 1440, 1000);
  const stage = page.locator(".remote-scene-stage");
  const startedAt = Date.now();
  const startingFrame = Number(await stage.getAttribute("data-playback-frame"));
  await page.waitForFunction(
    (start) => Number(document.querySelector(".remote-scene-stage")?.getAttribute("data-playback-frame")) >= start + 12,
    startingFrame,
    { timeout: 2_000 },
  );
  const advancingFrame = Number(await stage.getAttribute("data-playback-frame"));
  const initialTask = (await stage.getAttribute("data-work-task")) ?? "";
  const initialCycle = Number(await stage.getAttribute("data-work-cycle"));
  await page.waitForFunction(
    (cycle) => Number(document.querySelector(".remote-scene-stage")?.getAttribute("data-work-cycle")) > cycle,
    initialCycle,
    { timeout: 22_000 },
  );
  const loopedTask = (await stage.getAttribute("data-work-task")) ?? "";
  const loopedCycle = Number(await stage.getAttribute("data-work-cycle"));
  if (!initialTask || !loopedTask || initialTask === loopedTask || loopedCycle <= initialCycle) {
    throw new Error(`work loop did not advance story: ${initialTask}@${initialCycle} -> ${loopedTask}@${loopedCycle}`);
  }
  return {
    advancingFrame,
    loopedFrame: Number(await stage.getAttribute("data-playback-frame")),
    initialTask,
    loopedTask,
    initialCycle,
    loopedCycle,
    elapsedMs: Date.now() - startedAt,
  };
}

async function main(): Promise<void> {
  const response = await fetch(sceneUrl).catch(() => null);
  if (!response?.ok) {
    throw new Error(`Remote scene is not reachable at ${sceneUrl}. Start dev first or set FRONTEND_URL.`);
  }

  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on("pageerror", (error) => browserErrors.push(error.stack ?? error.message));
    page.on("console", (message) => {
      if (message.type() === "error") browserErrors.push(message.text());
    });
    page.on("requestfailed", (request) => failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText ?? "failed"}`));

    const playbackCheck = await verifyLivePlayback(page);
    const frameCount = Number(await page.locator(".remote-scene-stage").getAttribute("data-scene-frame-count"));
    const captureCount = Math.ceil(frameCount / sampleEvery) + 1;
    const desktopCaptures = await captureFrames(page, "desktop", 1440, 1000, captureCount, "stage", sampleEvery);
    const mobileSceneCaptures = await captureFrames(page, "mobile-scene", 393, 852, captureCount, "stage", sampleEvery);
    const mobilePageCaptures = await captureFrames(page, "mobile-page", 393, 852, 3, "viewport", Math.floor(frameCount / 2));
    const groups: CaptureGroup[] = [
      {
        name: "desktop",
        width: 1440,
        height: 1000,
        captureMode: "stage",
        captures: desktopCaptures,
        contactSheet: await createContactSheet(browser, "desktop", desktopCaptures, 3),
      },
      {
        name: "mobile-scene",
        width: 393,
        height: 852,
        captureMode: "stage",
        captures: mobileSceneCaptures,
        contactSheet: await createContactSheet(browser, "mobile-scene", mobileSceneCaptures, 3),
      },
      {
        name: "mobile-page",
        width: 393,
        height: 852,
        captureMode: "viewport",
        captures: mobilePageCaptures,
        contactSheet: await createContactSheet(browser, "mobile-page", mobilePageCaptures, 3),
      },
    ];

    writeReviewFiles(groups, frameCount, playbackCheck);
    await page.close();

    if (browserErrors.length || failedRequests.length) {
      throw new Error(`Capture completed with ${browserErrors.length} browser errors and ${failedRequests.length} failed requests. See manifest.json.`);
    }
    console.log(JSON.stringify({ outputDir, frameCount, sampleEvery, playbackCheck, groups }, null, 2));
  } finally {
    await browser.close();
  }
}

await main();
