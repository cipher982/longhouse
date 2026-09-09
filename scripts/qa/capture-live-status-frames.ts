#!/usr/bin/env bun
/** Exercise the real-renderer design lab, not the hosted session.
 * Start make live-status-lab, then:
 * make capture-live-status-frames CAPTURE=/private/session.json
 * Output is private (real transcript screenshots/video); never commit it.
 */
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium, type Page } from "playwright";

const args = process.argv.slice(2);
function option(name: string, fallback = ""): string {
  const index = args.indexOf(name);
  return index < 0 ? fallback : (args[index + 1] ?? fallback);
}
const capture = resolve(option("--capture"));
if (!option("--capture"))
  throw new Error(
    "--capture is required (private JSON from make capture-live-status)",
  );
const url = option("--url", "http://127.0.0.1:47213/live-status-lab.html");
const output = resolve(
  option(
    "--output",
    `artifacts/live-status-lab/frames-${new Date().toISOString().replace(/[:.]/g, "-")}`,
  ),
);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const errors: string[] = [];
const evidence: Array<Record<string, unknown>> = [];

async function controls(page: Page, open: boolean) {
  await page
    .getByTestId("lab-controls-disclosure")
    .evaluate((element, value) => {
      if (window.matchMedia("(max-width: 600px)").matches)
        (element as HTMLDetailsElement).open = value;
    }, open);
}
async function seek(page: Page, scene: string, time: number) {
  await controls(page, true);
  await page.getByLabel("Scenario", { exact: true }).selectOption(scene);
  await page
    .getByLabel("Replay time", { exact: true })
    .evaluate((element, value) => {
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )!.set!;
      setter.call(element, String(value));
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
    }, time);
  await page.waitForFunction(
    ({ scene, time }) => {
      const app = document.querySelector(".lab-app");
      return (
        app?.getAttribute("data-scene") === scene &&
        Number(app?.getAttribute("data-time-ms")) === time
      );
    },
    { scene, time },
  );
  await controls(page, false);
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement)
      document.activeElement.blur();
  });
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
}
async function snapshot(page: Page, name: string, scene: string, time: number) {
  await seek(page, scene, time);
  const ribbon = page.getByTestId("live-work-ribbon");
  const observation = await ribbon.innerText();
  const work = await ribbon.getAttribute("data-work-motion");
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > innerWidth + 1,
  );
  assert.equal(overflow, false, `${name}: horizontal page overflow`);
  const layout = await page.evaluate(() => ({
    composerHeight: document
      .querySelector(".lab-composer")!
      .getBoundingClientRect().height,
    transcriptHeight: document
      .querySelector(".timeline-events")!
      .getBoundingClientRect().height,
  }));
  await page.screenshot({ path: `${output}/${name}.png`, fullPage: false });
  evidence.push({
    name,
    scene,
    time,
    work,
    observation,
    horizontalOverflow: overflow,
    ...layout,
  });
  return { work, observation };
}
try {
  for (const size of [
    { name: "desktop", width: 1440, height: 1000 },
    { name: "mobile", width: 390, height: 844 },
    { name: "compact-large", width: 320, height: 740 },
  ]) {
    const context = await browser.newContext({
      viewport: size,
      deviceScaleFactor: 1,
      reducedMotion: "no-preference",
      ...(size.name === "desktop"
        ? {
            recordVideo: {
              dir: `${output}/video`,
              size: { width: 1440, height: 1000 },
            },
          }
        : {}),
    });
    try {
      const page = await context.newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      await context.route("**/*", (route) => {
        const request = route.request();
        const parsed = new URL(request.url());
        if (
          (parsed.protocol === "http:" || parsed.protocol === "https:") &&
          parsed.origin !== new URL(url).origin
        )
          return route.abort();
        if (request.url().includes("/api/")) return route.abort();
        return route.continue();
      });
      await page.goto(url);
      await page.locator("#capture-file").setInputFiles(capture);
      await page.getByTestId("lab-ready").waitFor();
      if (size.name === "compact-large") {
        await controls(page, true);
        await page.getByLabel("Larger text", { exact: true }).check();
      }
      const active = await snapshot(
        page,
        `${size.name}-working`,
        "working",
        4000,
      );
      assert.equal(
        active.work,
        "active",
        "Fresh simulated work should animate",
      );
      const draft = page.getByRole("textbox", { name: "Draft message" });
      const restingComposer = await page.locator(".lab-composer").boundingBox();
      assert.ok(
        restingComposer && restingComposer.height <= 120,
        "The resting activity and composer must leave room for the transcript",
      );
      await draft.focus();
      const editingComposer = await page.locator(".lab-composer").boundingBox();
      assert.ok(
        editingComposer && editingComposer.height > restingComposer.height,
        "The composer must expand for editing",
      );
      await draft.fill("Keep this draft.\nWait for my next instruction.");
      await page.screenshot({ path: `${output}/${size.name}-editing.png` });
      const disclosure = page
        .getByTestId("live-work-ribbon")
        .locator("summary");
      await disclosure.click();
      const editingDraft = await draft.boundingBox();
      assert.ok(
        editingDraft && editingDraft.y + editingDraft.height <= size.height,
        "Expanded evidence must leave the active draft onscreen",
      );
      await page.screenshot({
        path: `${output}/${size.name}-editing-details.png`,
      });
      await disclosure.click();
      assert.equal(
        await draft.inputValue(),
        "Keep this draft.\nWait for my next instruction.",
      );
      await draft.fill("");
      await draft.press("Shift+Tab");
      const restoredComposer = await page
        .locator(".lab-composer")
        .boundingBox();
      assert.equal(
        restoredComposer?.height,
        restingComposer.height,
        "An empty blurred draft must return to its compact size",
      );
      evidence.push({
        name: `${size.name}-composer-interaction`,
        restingHeight: restingComposer.height,
        editingHeight: editingComposer.height,
        draftRetained: true,
      });
      const quiet = await snapshot(
        page,
        `${size.name}-healthy-silence`,
        "quiet",
        22000,
      );
      assert.equal(
        quiet.work,
        "active",
        "Silence must not invent inactivity while evidence is valid",
      );
      const expired = await snapshot(
        page,
        `${size.name}-expired`,
        "expiry",
        12050,
      );
      assert.equal(
        expired.work,
        "off",
        "Expired work authority must stop work motion",
      );
      assert.match(expired.observation, /unconfirmed/i);
      const offline = await snapshot(
        page,
        `${size.name}-disconnected`,
        "reconnect",
        5050,
      );
      assert.equal(offline.work, "off");
      assert.match(offline.observation, /reconnecting/i);
      const replay = await snapshot(
        page,
        `${size.name}-replaying`,
        "reconnect",
        14500,
      );
      assert.equal(replay.work, "off", "Old data replay must not animate work");
      const originalMark = page.locator('[data-receipt-id="receipt-4400"]');
      if (await originalMark.count()) {
        assert.equal(
          await originalMark.getAttribute("data-replay"),
          "false",
          "Reconnect must not rewrite earlier receipt provenance",
        );
      }
      const replayMark = page.locator('[data-receipt-id="receipt-12200"]');
      if (await replayMark.count())
        assert.equal(await replayMark.getAttribute("data-replay"), "true");
      const restored = await snapshot(
        page,
        `${size.name}-restored`,
        "reconnect",
        20500,
      );
      assert.equal(restored.work, "active");
      await snapshot(page, `${size.name}-machine-unreachable`, "machine", 9000);
      await snapshot(page, `${size.name}-foreground-check`, "return", 3000);
      await snapshot(page, `${size.name}-approval`, "attention", 8000);
      await snapshot(page, `${size.name}-finished`, "finished", 12000);
      const recorded = await snapshot(
        page,
        `${size.name}-recorded`,
        "recorded",
        0,
      );
      assert.equal(
        recorded.work,
        "off",
        "Recording never claims a live connection",
      );
      await controls(page, true);
      await page.getByLabel("Reduce live motion", { exact: true }).check();
      const reduced = await snapshot(
        page,
        `${size.name}-reduced-motion`,
        "working",
        4000,
      );
      assert.equal(reduced.work, "off");
      await seek(page, "working", 7000);
      const stableMark = page.locator('[data-receipt-id="receipt-650"]');
      const beforeMotion = await stableMark.boundingBox();
      assert.ok(
        beforeMotion,
        "Capture needs at least one replayable transcript item",
      );
      await seek(page, "working", 9000);
      const afterMotion = await stableMark.boundingBox();
      assert.ok(afterMotion);
      assert.equal(
        afterMotion.x,
        beforeMotion.x,
        "Reduced-motion receipt positions must remain stationary",
      );
      await page.screenshot({
        path: `${output}/${size.name}-reduced-stationary.png`,
      });
      evidence.push({
        name: `${size.name}-reduced-stationary`,
        sameReceiptX: afterMotion.x,
        passed: true,
      });
      await controls(page, true);
      await page.getByLabel("Reduce live motion", { exact: true }).uncheck();
      await page.emulateMedia({ reducedMotion: "reduce" });
      const systemReduced = await snapshot(
        page,
        `${size.name}-system-reduced`,
        "working",
        4000,
      );
      assert.equal(
        systemReduced.work,
        "off",
        "System motion preference must override a previous unchecked lab preference",
      );
      await page.emulateMedia({ reducedMotion: "no-preference" });
      await controls(page, true);
      await page.getByLabel("Theme", { exact: true }).selectOption("light");
      await snapshot(page, `${size.name}-light`, "working", 4000);
      await page.getByTestId("live-work-ribbon").locator("summary").click();
      await page.screenshot({
        path: `${output}/${size.name}-observation-details.png`,
      });
      const expandedDraft = await page
        .getByRole("textbox", { name: "Draft message" })
        .boundingBox();
      assert.ok(
        expandedDraft && expandedDraft.y + expandedDraft.height <= size.height,
        "Observation details must not push the composer offscreen",
      );
      await page.getByTestId("live-work-ribbon").locator("summary").click();
      if (size.name === "desktop") {
        await controls(page, true);
        await page.getByLabel("Theme", { exact: true }).selectOption("dark");
        await page.getByLabel("Reduce live motion", { exact: true }).uncheck();
        // Actual playback, not just seeking: retain a draft through loss/recovery.
        await page
          .getByRole("textbox", { name: "Draft message" })
          .fill("Keep this local draft through reconnect.");
        await seek(page, "reconnect", 3000);
        await page
          .getByRole("button", { name: "Play replay", exact: true })
          .click();
        await page.waitForFunction(
          () =>
            Number(
              document.querySelector(".lab-app")?.getAttribute("data-time-ms"),
            ) >= 19000,
          undefined,
          { timeout: 25000 },
        );
        await page
          .getByRole("button", { name: "Pause replay", exact: true })
          .click();
        assert.equal(
          await page
            .getByRole("textbox", { name: "Draft message" })
            .inputValue(),
          "Keep this local draft through reconnect.",
        );
        evidence.push({
          name: "real-time-disconnect-replay",
          passed: true,
          draftRetained: true,
        });
      }
    } finally {
      await context.close();
    }
  }
  assert.deepEqual(errors, [], "Browser runtime errors");
} finally {
  await browser.close();
  await writeFile(
    `${output}/manifest.json`,
    JSON.stringify(
      { capture, url, createdAt: new Date().toISOString(), evidence, errors },
      null,
      2,
    ),
  );
}
console.log(
  JSON.stringify({ output, frames: evidence.length, errors }, null, 2),
);
