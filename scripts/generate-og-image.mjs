/**
 * Generate the OpenGraph / social card (web/public/og-image.png, 1200x630).
 *
 * Code-derived, not hand-drawn: composes the wedge headline + the real
 * timeline screenshot + the master logo into an on-brand card, then
 * screenshots it. Re-run after changing the headline or the timeline shot.
 *
 *   node scripts/generate-og-image.mjs
 *
 * Requires playwright chromium (already used by the marketing capture flow).
 */
import { chromium } from "playwright";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "..");
const logoSvg = readFileSync(resolve(repo, "web/branding/longhouse-logo-master.svg"), "utf8");
const timelinePng = readFileSync(resolve(repo, "web/public/images/landing/timeline-preview.png"));
const timelineDataUri = `data:image/png;base64,${timelinePng.toString("base64")}`;
const out = resolve(repo, "web/public/og-image.png");

const GOLD = "#C9A66B";

const html = `<!doctype html><html><head><meta charset="utf-8"/>
<style>
  * { margin: 0; box-sizing: border-box; }
  html, body { width: 1200px; height: 630px; }
  body {
    display: flex; align-items: center; gap: 56px;
    padding: 72px;
    background: radial-gradient(120% 140% at 0% 0%, #1a1410 0%, #0a0807 60%);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #f4ecdd; overflow: hidden;
  }
  .left { width: 560px; flex: none; display: flex; flex-direction: column; gap: 26px; }
  .brand { display: flex; align-items: center; gap: 16px; }
  .brand svg { width: 52px; height: 52px; }
  .brand .name { font-size: 30px; font-weight: 700; letter-spacing: 0.04em; }
  .kicker { color: ${GOLD}; font-size: 19px; font-weight: 600; letter-spacing: 0.14em; text-transform: uppercase; }
  h1 { font-size: 58px; line-height: 1.05; font-weight: 700; }
  h1 .accent { color: ${GOLD}; }
  .sub { font-size: 23px; line-height: 1.4; color: rgba(244,236,221,0.66); }
  .shot {
    flex: 1; height: 486px; border-radius: 16px; overflow: hidden;
    border: 1px solid rgba(255,255,255,0.08);
    box-shadow: 0 30px 90px rgba(0,0,0,0.6);
  }
  .shot img { width: 100%; height: 100%; object-fit: cover; object-position: left top; display: block; }
</style></head>
<body>
  <div class="left">
    <div class="brand">${logoSvg}<span class="name">Longhouse</span></div>
    <div class="kicker">Self-hosted · cross-provider · yours</div>
    <h1>Start a coding agent. Walk away. <span class="accent">Steer it from your phone.</span></h1>
    <div class="sub">One timeline + live control for every Claude Code, Codex &amp; OpenCode session — on machines you own.</div>
  </div>
  <div class="shot"><img src="${timelineDataUri}"/></div>
</body></html>`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
await page.setContent(html, { waitUntil: "networkidle" });
await page.waitForTimeout(200);
await page.screenshot({ path: out });
await browser.close();
console.log(`og-image written: ${out}`);
