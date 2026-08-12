#!/usr/bin/env node
/**
 * QA harness for the landing page's real below-fold steering demo.
 *
 * The hero must remain the autoplay product story. The live sandbox belongs
 * in the separate SteerPlayground section, where a visitor edits the iPhone
 * composer and watches the real Claude Code terminal respond.
 *
 * Usage:
 *   cd e2e && node scripts/qa-landing-live-demo.mjs [url] [--run] [--shots DIR]
 *
 * `--run` executes one instruction in a real sandbox. It spends money and
 * consumes the per-visitor rate limit, so it is off by default.
 */
import { chromium } from "@playwright/test";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const args = process.argv.slice(2);
const url = args.find((arg) => arg.startsWith("http")) ?? "http://localhost:5173/landing";
const doRun = args.includes("--run");
const shotsFlag = args.indexOf("--shots");
const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "wide-short", width: 1800, height: 850 },
  { name: "mobile", width: 390, height: 844, isMobile: true },
];

const results = [];
let failures = 0;

function check(name, ok, detail) {
  results.push({ name, ok, detail });
  if (!ok) failures += 1;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` - ${detail}` : ""}`);
}

const shotsDir =
  shotsFlag >= 0 ? args[shotsFlag + 1] : await mkdtemp(path.join(tmpdir(), "landing-qa-"));

const browser = await chromium.launch();
try {
  for (const viewport of VIEWPORTS) {
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      isMobile: Boolean(viewport.isMobile),
      hasTouch: Boolean(viewport.isMobile),
    });
    const page = await context.newPage();
    try {
      // Exercise the real cold-load seam. Without a delay, a warm local cache
      // can hide a broken Suspense fallback that visitors still see in prod.
      await page.route(/\/HeroDemo(?:-[^/]+)?\.(?:js|tsx)(?:\?.*)?$/, async (route) => {
        await new Promise((resolve) => setTimeout(resolve, 700));
        await route.continue();
      });
      await page.goto(url, { waitUntil: "domcontentloaded" });

      const firstPaint = page.locator(".landing-hero .hero-demo-fallback");
      await firstPaint.waitFor({ state: "visible", timeout: 5000 }).catch(() => {});
      const firstPaintState = await page.evaluate(() => {
        const fallback = document.querySelector(".landing-hero .hero-demo-fallback");
        if (!fallback) return null;
        const terminals = fallback.querySelectorAll(".hero-demo-terminal");
        const titles = fallback.querySelectorAll(".hero-demo-terminal-title");
        const rect = fallback.getBoundingClientRect();
        return {
          terminals: terminals.length,
          titles: titles.length,
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        };
      });
      check(
        `${viewport.name}: cold load paints the terminal deck`,
        Boolean(
          firstPaintState &&
          firstPaintState.terminals === 3 &&
          firstPaintState.titles === 3 &&
          firstPaintState.width >= 300 &&
          firstPaintState.height >= 180
        ),
        firstPaintState
          ? `${firstPaintState.terminals} terminals, ${firstPaintState.width}×${firstPaintState.height}`
          : "fallback not painted",
      );
      await page.screenshot({
        path: path.join(shotsDir, `${viewport.name}-hero-first-paint.png`),
      });

      const hero = page.locator(".landing-hero .hero-demo");
      await hero.waitFor({ state: "visible", timeout: 20000 });
      await page
        .waitForFunction(
          () =>
            Array.from(document.querySelectorAll(".landing-hero .hero-demo-beat")).some((beat) => {
              const rect = beat.getBoundingClientRect();
              return getComputedStyle(beat).visibility === "visible" && rect.height >= 180;
            }),
          null,
          { timeout: 10000 },
        )
        .catch(() => {});
      const heroPaint = await page.evaluate(() => {
        const demo = document.querySelector(".landing-hero .hero-demo");
        const visibleBeat = Array.from(
          document.querySelectorAll(".landing-hero .hero-demo-beat"),
        ).find((beat) => getComputedStyle(beat).visibility === "visible");
        if (!demo || !visibleBeat) return null;
        const demoRect = demo.getBoundingClientRect();
        const beatRect = visibleBeat.getBoundingClientRect();
        return {
          demoWidth: Math.round(demoRect.width),
          beatHeight: Math.round(beatRect.height),
        };
      });
      check(
        `${viewport.name}: autoplay demo remains in the hero`,
        Boolean(heroPaint && heroPaint.demoWidth >= 300 && heroPaint.beatHeight >= 180),
        heroPaint ? `${heroPaint.demoWidth}px wide, ${heroPaint.beatHeight}px tall` : "not painted",
      );
      check(
        `${viewport.name}: hero has no live-demo toggle`,
        (await page.locator(".landing-hero .hero-demo-modeswitch").count()) === 0,
      );
      await page.screenshot({ path: path.join(shotsDir, `${viewport.name}-hero.png`) });

      const playground = page.locator(".steer-playground");
      await playground.scrollIntoViewIfNeeded();
      await playground.hover();

      const showedCommand = page
        .waitForFunction(
          () =>
            (document.querySelector(".steer-playground .xterm-rows")?.textContent ?? "")
              .replace(/\s+/g, "")
              .includes("claude--dangerously-skip-permissions"),
          null,
          { timeout: 90000 },
        )
        .then(() => true)
        .catch(() => false);

      const state = page.locator(".steer-playground .phone-session-state");
      await state.waitFor({ state: "visible", timeout: 15000 });
      await page
        .waitForFunction(
          () => /ready|unavailable/i.test(
            document.querySelector(".steer-playground .phone-session-state")?.textContent ?? "",
          ),
          null,
          { timeout: 90000 },
        )
        .catch(() => {});
      const stateText = (await state.innerText()).trim();
      check(`${viewport.name}: sandbox reaches ready`, /ready/i.test(stateText), stateText);

      const input = page.getByRole("textbox", { name: "Message to live session" });
      check(`${viewport.name}: phone composer is editable`, await input.isEditable());

      const rows =
        (await page.locator(".steer-playground .xterm-rows").innerText().catch(() => "")) || "";
      check(`${viewport.name}: no su/job-control noise`, !/su -p demo|job control/.test(rows));
      check(`${viewport.name}: shows the short claude command`, await showedCommand);

      const fill = await page.evaluate(() => {
        const pane = document.querySelector(".steer-playground .hero-live-terminal");
        const screen = document.querySelector(".steer-playground .xterm-screen");
        if (!pane || !screen) return 0;
        return Math.round(
          (100 * screen.getBoundingClientRect().width) / pane.getBoundingClientRect().width,
        );
      });
      check(`${viewport.name}: terminal fills the frame`, fill >= 90, `${fill}%`);

      const phone = page.locator(".steer-playground .phone-frame");
      await phone.scrollIntoViewIfNeeded();
      await page.waitForTimeout(300);
      const sendFold = await page.evaluate(() => {
        const send = document.querySelector(".steer-playground .phone-session-send");
        if (!send) return null;
        return { bottom: Math.round(send.getBoundingClientRect().bottom), vh: window.innerHeight };
      });
      check(
        `${viewport.name}: phone send control above the fold`,
        Boolean(sendFold) && sendFold.bottom <= sendFold.vh,
        sendFold ? `y=${sendFold.bottom} of ${sendFold.vh}` : "no send control",
      );

      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      );
      check(`${viewport.name}: no horizontal overflow`, overflow);

      if (doRun && viewport.name === "desktop") {
        const send = page.getByRole("button", { name: "Send" });
        await send.click();
        await page
          .waitForFunction(
            () => /complete|unavailable/i.test(
              document.querySelector(".steer-playground .phone-session-state")?.textContent ?? "",
            ),
            null,
            { timeout: 120000 },
          )
          .catch(() => {});
        const finalText = (await state.innerText()).trim();
        check(`${viewport.name}: instruction completes`, /complete/i.test(finalText), finalText);
      }

      await playground.scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(shotsDir, `${viewport.name}.png`) });
      if (viewport.name === "mobile") {
        await page.locator(".steer-live-terminal").scrollIntoViewIfNeeded();
        await page.screenshot({ path: path.join(shotsDir, "mobile-terminal.png") });
      }
    } finally {
      await context.close();
    }
  }
} finally {
  await browser.close();
}

console.log(`\nscreenshots: ${shotsDir}`);
console.log(`${results.length - failures}/${results.length} checks passed`);
process.exit(failures === 0 ? 0 : 1);
