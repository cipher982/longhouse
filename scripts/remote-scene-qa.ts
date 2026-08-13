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
  finalFrame: number;
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
const variant = argValue("variant");
if (variant && variant !== "inset" && variant !== "cutin") {
  throw new Error("--variant must be inset or cutin");
}
const sceneUrl = `${baseUrl}/prototypes/remote-scene${variant ? `?terminal=${variant}` : ""}`;

mkdirSync(outputDir, { recursive: true });

const browserErrors: string[] = [];
const failedRequests: string[] = [];

function sampledFrames(frameCount: number, step: number): number[] {
  const values = new Set<number>();
  for (let frame = 0; frame < frameCount; frame += step) values.add(frame);
  values.add(Math.max(0, frameCount - 1));
  return [...values].sort((left, right) => left - right);
}

async function settleFrame(page: Page): Promise<void> {
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  await page.waitForTimeout(40);
}

async function captureFrames(
  page: Page,
  groupName: string,
  width: number,
  height: number,
  frames: number[],
  captureMode: "stage" | "viewport",
): Promise<Capture[]> {
  await page.setViewportSize({ width, height });
  await page.goto(sceneUrl, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);

  const slider = page.getByRole("slider", { name: "Scrub remote control scene" });
  const stage = page.locator(".remote-scene-stage");
  await stage.waitFor({ state: "visible" });

  const captures: Capture[] = [];
  for (const frame of frames) {
    await slider.fill(String(frame));
    await settleFrame(page);
    const filename = `${groupName}-frame-${String(frame).padStart(3, "0")}.png`;
    const absolutePath = path.join(outputDir, filename);
    if (captureMode === "viewport") {
      await stage.evaluate((element) => {
        const top = element.getBoundingClientRect().top + window.scrollY;
        window.scrollTo({ top: Math.max(0, top - 18), behavior: "auto" });
      });
      await settleFrame(page);
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
    schemaVersion: 1,
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

async function verifyLivePlayback(page: Page, lastFrame: number): Promise<PlaybackCheck> {
  const slider = page.getByRole("slider", { name: "Scrub remote control scene" });
  await slider.fill("0");
  await settleFrame(page);
  const startedAt = Date.now();
  await page.getByRole("button", { name: "Play scene" }).click();
  await page.waitForFunction(
    () => Number((document.querySelector('input[aria-label="Scrub remote control scene"]') as HTMLInputElement)?.value ?? 0) >= 12,
    undefined,
    { timeout: 2_000 },
  );
  const advancingFrame = Number(await slider.inputValue());
  await page.waitForFunction(
    (targetFrame) => Number((document.querySelector('input[aria-label="Scrub remote control scene"]') as HTMLInputElement)?.value ?? 0) >= targetFrame,
    lastFrame,
    { timeout: 8_000 },
  );
  return {
    advancingFrame,
    finalFrame: Number(await slider.inputValue()),
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

    await page.goto(sceneUrl, { waitUntil: "networkidle" });
    const slider = page.getByRole("slider", { name: "Scrub remote control scene" });
    const lastFrame = Number.parseInt((await slider.getAttribute("max")) ?? "0", 10);
    const frameCount = lastFrame + 1;
    const playbackCheck = await verifyLivePlayback(page, lastFrame);
    const desktopFrames = sampledFrames(frameCount, sampleEvery);

    const desktopCaptures = await captureFrames(page, "desktop", 1440, 1000, desktopFrames, "stage");
    const mobileSceneCaptures = await captureFrames(page, "mobile-scene", 393, 852, desktopFrames, "stage");
    const mobilePageFrames = [...new Set([0, Math.floor(lastFrame / 2), lastFrame])];
    const mobilePageCaptures = await captureFrames(page, "mobile-page", 393, 852, mobilePageFrames, "viewport");
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
    console.log(JSON.stringify({ outputDir, frameCount, sampleEvery, groups }, null, 2));
  } finally {
    await browser.close();
  }
}

await main();
