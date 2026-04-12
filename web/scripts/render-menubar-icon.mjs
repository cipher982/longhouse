#!/usr/bin/env node

import { chromium } from "playwright";
import { readFile } from "node:fs/promises";
import path from "node:path";

function usage() {
  console.error("Usage: render-menubar-icon.mjs <master.svg> <output.png> <width> <height>");
  process.exit(2);
}

const [, , inputPath, outputPath, widthRaw, heightRaw] = process.argv;

if (!inputPath || !outputPath || !widthRaw || !heightRaw) {
  usage();
}

const width = Number(widthRaw);
const height = Number(heightRaw);

if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
  usage();
}

const masterPath = path.resolve(inputPath);
const masterSvg = await readFile(masterPath, "utf8");

function deriveBrightMonochromeMenubarSvg(svg) {
  let derived = svg;

  // Keep one geometry source of truth, but remap the full-color logo into a
  // menu-bar-specific bright white/ink palette that preserves visor/detail separation.
  derived = derived.replace(/<defs>[\s\S]*?<\/defs>/, "");
  derived = derived.replaceAll('fill="url(#helmetGrad)"', 'fill="#F8FAFC"');
  derived = derived.replaceAll('stroke="#2E1A62"', 'stroke="#7C8795"');
  derived = derived.replaceAll('fill="url(#visorGrad)"', 'fill="#0B0F14"');
  derived = derived.replace('stroke="#8EF0AA"', 'stroke="#FFFFFF"');
  derived = derived.replace('stroke="#AAFFD0"', 'stroke="#FFFFFF"');
  derived = derived.replaceAll('fill="#9AF7A8"', 'fill="#FFFFFF"');
  derived = derived.replaceAll('stroke="#9AF7A8"', 'stroke="#FFFFFF"');
  derived = derived.replace('fill="url(#highlightGrad)"', 'fill="rgba(255,255,255,0.22)"');
  derived = derived.replace('fill="#ffffff" opacity="0.14"', 'fill="rgba(255,255,255,0.18)"');

  // The master viewBox is already tight to content bounds.
  // For the menu bar, nudge slightly if needed for optical centering.
  // Currently the master crop works well for the menu bar slot.

  return derived;
}

const svg = deriveBrightMonochromeMenubarSvg(masterSvg);

const browser = await chromium.launch({ headless: true });

try {
  const page = await browser.newPage({
    viewport: { width, height },
    deviceScaleFactor: 1,
  });

  await page.setContent(
    `<!doctype html>
    <html>
      <body style="margin:0; width:${width}px; height:${height}px; display:grid; place-items:center; background:transparent; overflow:hidden;">
        <style>
          html, body {
            width: ${width}px;
            height: ${height}px;
          }

          .asset-frame {
            width: ${width}px;
            height: ${height}px;
            display: grid;
            place-items: center;
          }

          .asset-frame > svg {
            width: 100%;
            height: 100%;
            display: block;
          }
        </style>
        <div class="asset-frame">
          ${svg}
        </div>
      </body>
    </html>`,
    { waitUntil: "load" }
  );

  await page.screenshot({
    path: path.resolve(outputPath),
    omitBackground: true,
  });
} finally {
  await browser.close();
}
