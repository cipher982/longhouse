#!/usr/bin/env node

import { chromium } from "playwright";
import { readFile } from "node:fs/promises";
import path from "node:path";

function usage() {
  console.error("Usage: render-menubar-icon.mjs <master.svg> <output.png> <width> <height> [tone]");
  process.exit(2);
}

const [, , inputPath, outputPath, widthRaw, heightRaw, toneRaw] = process.argv;

if (!inputPath || !outputPath || !widthRaw || !heightRaw) {
  usage();
}

const width = Number(widthRaw);
const height = Number(heightRaw);
const tone = toneRaw ?? "menu";
const opticalInsetPx = Math.max(1, Math.round(Math.min(width, height) * 0.03));
const verticalOffsetPx = opticalInsetPx;
const innerWidth = width - opticalInsetPx * 2;
const innerHeight = height - opticalInsetPx * 2;

if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
  usage();
}

const masterPath = path.resolve(inputPath);
const masterSvg = await readFile(masterPath, "utf8");

function paletteForTone(rawTone) {
  switch (rawTone) {
    case "green":
      return {
        shellFill: "#61D88C",
        shellStroke: "#236842",
        visorFill: "#0A1711",
        visorStroke: "#C7FFE0",
        detailFill: "#F1FFF8",
        detailStroke: "#F1FFF8",
        highlightFill: "rgba(255,255,255,0.22)",
        sparkleFill: "rgba(255,255,255,0.14)",
      };
    case "yellow":
      return {
        shellFill: "#E0B455",
        shellStroke: "#7E5618",
        visorFill: "#1E1408",
        visorStroke: "#FFE2A1",
        detailFill: "#FFF3CF",
        detailStroke: "#FFF3CF",
        highlightFill: "rgba(255,255,255,0.18)",
        sparkleFill: "rgba(255,255,255,0.12)",
      };
    case "red":
      return {
        shellFill: "#D96C58",
        shellStroke: "#732019",
        visorFill: "#210C0A",
        visorStroke: "#FFC0B3",
        detailFill: "#FFE4DE",
        detailStroke: "#FFE4DE",
        highlightFill: "rgba(255,255,255,0.16)",
        sparkleFill: "rgba(255,255,255,0.10)",
      };
    case "gray":
      return {
        shellFill: "#AAB4BE",
        shellStroke: "#495360",
        visorFill: "#0D131A",
        visorStroke: "#E3EDF7",
        detailFill: "#F5F9FD",
        detailStroke: "#F5F9FD",
        highlightFill: "rgba(255,255,255,0.15)",
        sparkleFill: "rgba(255,255,255,0.10)",
      };
    case "menu":
      return {
        shellFill: "#F8FAFC",
        shellStroke: "#7C8795",
        visorFill: "#0B0F14",
        visorStroke: "#FFFFFF",
        detailFill: "#FFFFFF",
        detailStroke: "#FFFFFF",
        highlightFill: "rgba(255,255,255,0.22)",
        sparkleFill: "rgba(255,255,255,0.18)",
      };
    default:
      usage();
  }
}

function deriveMenubarSvg(svg, rawTone) {
  let derived = svg;
  const palette = paletteForTone(rawTone);

  // Keep one geometry source of truth, but remap the full-color logo into
  // a compact severity palette that preserves shell/visor/detail separation.
  derived = derived.replace(/<defs>[\s\S]*?<\/defs>/, "");
  derived = derived.replaceAll('fill="url(#helmetGrad)"', `fill="${palette.shellFill}"`);
  derived = derived.replaceAll('stroke="#2E1A62"', `stroke="${palette.shellStroke}"`);
  derived = derived.replaceAll('fill="url(#visorGrad)"', `fill="${palette.visorFill}"`);
  derived = derived.replace('stroke="#8EF0AA"', `stroke="${palette.visorStroke}"`);
  derived = derived.replace('stroke="#AAFFD0"', `stroke="${palette.visorStroke}"`);
  derived = derived.replaceAll('fill="#9AF7A8"', `fill="${palette.detailFill}"`);
  derived = derived.replaceAll('stroke="#9AF7A8"', `stroke="${palette.detailStroke}"`);
  derived = derived.replace('fill="url(#highlightGrad)"', `fill="${palette.highlightFill}"`);
  derived = derived.replace('fill="#ffffff" opacity="0.14"', `fill="${palette.sparkleFill}"`);

  // The master viewBox is already tight to content bounds.
  // The menu bar needs a tiny optical inset and downward nudge so the
  // helmet reads centered at 18pt without clipping the crown.

  return derived;
}

const svg = deriveMenubarSvg(masterSvg, tone);

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
            width: ${innerWidth}px;
            height: ${innerHeight}px;
            display: block;
            transform: translateY(${verticalOffsetPx}px);
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
