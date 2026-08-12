#!/usr/bin/env node
/**
 * QA harness for the landing page's live "Type your own" demo.
 *
 * Why this exists rather than ad-hoc CDP against a managed browser profile:
 * the `background`/`watchable` profiles in agent-browser-profile are for
 * interactive, identity-bearing browsing. Borrowing one for repeated UI QA
 * opens a window on the operator's screen, litters a SHARED profile with tabs
 * nobody can safely close, and collides with other agents holding the same
 * profile. This target is an unauthenticated page, so it needs no identity at
 * all — just a disposable browser.
 *
 * Properties this guarantees:
 *   - headless: never renders on anyone's screen
 *   - a fresh throwaway profile per run, isolated from every managed profile
 *   - teardown in `finally`, so no tab or browser survives a failure
 *   - fold/fill assertions from the landing-hero layout contract, measured
 *     rather than eyeballed
 *
 * Usage:
 *   cd e2e && node scripts/qa-landing-live-demo.mjs [url] [--run] [--shots DIR]
 *
 * `--run` actually executes an instruction in a real sandbox, which spends
 * money and consumes the per-visitor rate limit. Off by default.
 */
import { chromium } from "@playwright/test";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const args = process.argv.slice(2);
const url = args.find((a) => a.startsWith("http")) ?? "http://localhost:5173/landing";
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
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
}

const shotsDir =
  shotsFlag >= 0 ? args[shotsFlag + 1] : await mkdtemp(path.join(tmpdir(), "landing-qa-"));

const browser = await chromium.launch(); // headless by default
try {
  for (const vp of VIEWPORTS) {
    const context = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      isMobile: Boolean(vp.isMobile),
      hasTouch: Boolean(vp.isMobile),
    });
    const page = await context.newPage();
    try {
      await page.goto(url, { waitUntil: "domcontentloaded" });
      const shell = page.locator(".hero-demo-shell");
      await shell.waitFor({ state: "visible", timeout: 20000 });

      // Recorded is the default and must stay so.
      // innerText is uppercased by CSS, so compare case-insensitively.
      const badge = (await page.locator(".hero-demo-badge").innerText()).trim();
      check(`${vp.name}: opens on the recorded demo`, /^recorded demo$/i.test(badge), badge);

      // Real hover on the card is what triggers pre-warm.
      await shell.hover();
      await page.waitForTimeout(4000);
      await page.locator(".hero-demo-modeswitch").click();

      const status = page.locator(".hero-live-status");
      await status.waitFor({ state: "visible", timeout: 15000 });
      await page
        .waitForFunction(
          () => /ready|unavailable/i.test(document.querySelector(".hero-live-status")?.textContent ?? ""),
          null,
          { timeout: 90000 },
        )
        .catch(() => {});
      const readyText = (await status.innerText()).trim();
      check(`${vp.name}: session reaches ready`, /ready/i.test(readyText), readyText);

      // The plumbing must never be on screen.
      const rows = (await page.locator(".xterm-rows").innerText().catch(() => "")) || "";
      check(`${vp.name}: no su/job-control noise`, !/su -p demo|job control/.test(rows));
      // Strip ALL whitespace: the terminal wraps mid-word, so the command can
      // arrive as "--dangerously-skip-perm" + "issions" across two rows.
      check(
        `${vp.name}: shows the short claude command`,
        rows.replace(/\s+/g, "").includes("claude--dangerously-skip-permissions"),
      );

      // Terminal fills its frame rather than wrapping at someone else's width.
      const fill = await page.evaluate(() => {
        const pane = document.querySelector(".hero-live-terminal");
        const screen = document.querySelector(".xterm-screen");
        if (!pane || !screen) return 0;
        return Math.round((100 * screen.getBoundingClientRect().width) / pane.getBoundingClientRect().width);
      });
      check(`${vp.name}: terminal fills the frame`, fill >= 90, `${fill}%`);

      // Layout contract: scroll the SECTION to the top, then assert controls fit.
      await shell.evaluate((el) => el.scrollIntoView({ block: "start" }));
      await page.waitForTimeout(400);
      const fold = await page.evaluate(() => {
        const run = document.querySelector(".hero-live-run");
        if (!run) return null;
        return { bottom: Math.round(run.getBoundingClientRect().bottom), vh: window.innerHeight };
      });
      check(
        `${vp.name}: run control above the fold`,
        Boolean(fold) && fold.bottom <= fold.vh,
        fold ? `y=${fold.bottom} of ${fold.vh}` : "no control",
      );

      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      );
      check(`${vp.name}: no horizontal overflow`, overflow);

      if (doRun && vp.name === "desktop") {
        await page.locator(".hero-live-run").click();
        await page
          .waitForFunction(
            () => /finished|unavailable/i.test(document.querySelector(".hero-live-status")?.textContent ?? ""),
            null,
            { timeout: 120000 },
          )
          .catch(() => {});
        const finalText = (await status.innerText()).trim();
        check(`${vp.name}: instruction completes`, /finished/i.test(finalText), finalText);
      }

      await page.screenshot({ path: path.join(shotsDir, `${vp.name}.png`) });
    } finally {
      await context.close(); // every tab dies with its context
    }
  }
} finally {
  await browser.close(); // and the whole throwaway profile with it
}

console.log(`\nscreenshots: ${shotsDir}`);
console.log(`${results.length - failures}/${results.length} checks passed`);
process.exit(failures === 0 ? 0 : 1);
