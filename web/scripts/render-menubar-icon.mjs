#!/usr/bin/env node

import { chromium } from "playwright";
import { readFile } from "node:fs/promises";
import { mkdtemp, rm } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import os from "node:os";
import path from "node:path";

function usage() {
  console.error("Usage: render-menubar-icon.mjs <master.svg> <output.png> <width> <height> [tone] [paddingRatio]");
  process.exit(2);
}

const [, , inputPath, outputPath, widthRaw, heightRaw, toneRaw, paddingRatioRaw] = process.argv;

if (!inputPath || !outputPath || !widthRaw || !heightRaw) {
  usage();
}

const width = Number(widthRaw);
const height = Number(heightRaw);
const tone = toneRaw ?? "menu";
const paddingRatio = paddingRatioRaw == null ? 0 : Number(paddingRatioRaw);

if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0 || !Number.isFinite(paddingRatio) || paddingRatio < 0 || paddingRatio >= 0.5) {
  usage();
}

const masterPath = path.resolve(inputPath);
const masterSvg = await readFile(masterPath, "utf8");

function paletteForTone(rawTone) {
  switch (rawTone) {
    case "green":
      return {
        shellStart: "#8DECAF",
        shellMid: "#58C97D",
        shellEnd: "#2F8A53",
        shellStroke: "#236842",
        visorStart: "#1A2D22",
        visorEnd: "#0A1711",
        visorStroke: "#C7FFE0",
        detailFill: "#E9FFF1",
        detailStroke: "#C7FFE0",
        highlightStart: "#FFFFFF",
        highlightStartOpacity: "0.28",
        highlightEnd: "#FFFFFF",
        highlightEndOpacity: "0",
        sparkleFill: "rgba(255,255,255,0.18)",
      };
    case "yellow":
      return {
        shellStart: "#F2D98F",
        shellMid: "#D8AD4A",
        shellEnd: "#9B711E",
        shellStroke: "#7E5618",
        visorStart: "#342311",
        visorEnd: "#1E1408",
        visorStroke: "#FFE2A1",
        detailFill: "#FFF4CF",
        detailStroke: "#FFE2A1",
        highlightStart: "#FFFFFF",
        highlightStartOpacity: "0.24",
        highlightEnd: "#FFFFFF",
        highlightEndOpacity: "0",
        sparkleFill: "rgba(255,255,255,0.14)",
      };
    case "red":
      return {
        shellStart: "#F0998A",
        shellMid: "#D16552",
        shellEnd: "#922F24",
        shellStroke: "#732019",
        visorStart: "#341412",
        visorEnd: "#210C0A",
        visorStroke: "#FFC0B3",
        detailFill: "#FFE5DF",
        detailStroke: "#FFC0B3",
        highlightStart: "#FFFFFF",
        highlightStartOpacity: "0.22",
        highlightEnd: "#FFFFFF",
        highlightEndOpacity: "0",
        sparkleFill: "rgba(255,255,255,0.12)",
      };
    case "gray":
      return {
        shellStart: "#D4DCE5",
        shellMid: "#A9B4BF",
        shellEnd: "#6D7885",
        shellStroke: "#495360",
        visorStart: "#1C242D",
        visorEnd: "#0D131A",
        visorStroke: "#E3EDF7",
        detailFill: "#F4F9FD",
        detailStroke: "#E3EDF7",
        highlightStart: "#FFFFFF",
        highlightStartOpacity: "0.20",
        highlightEnd: "#FFFFFF",
        highlightEndOpacity: "0",
        sparkleFill: "rgba(255,255,255,0.12)",
      };
    case "menu":
      return {
        shellStart: "#FFFFFF",
        shellMid: "#EEF3F8",
        shellEnd: "#C9D3DE",
        shellStroke: "#7C8795",
        visorStart: "#1C2128",
        visorEnd: "#0B0F14",
        visorStroke: "#FFFFFF",
        detailFill: "#FFFFFF",
        detailStroke: "#FFFFFF",
        highlightStart: "#FFFFFF",
        highlightStartOpacity: "0.24",
        highlightEnd: "#FFFFFF",
        highlightEndOpacity: "0",
        sparkleFill: "rgba(255,255,255,0.18)",
      };
    default:
      usage();
  }
}

function deriveMenubarSvg(svg, rawTone) {
  let derived = svg;
  const palette = paletteForTone(rawTone);

  // Keep one geometry source of truth and recolor the existing gradients
  // rather than flattening the mark into a single fill.
  derived = derived.replace('stop-color="#A883FF"', `stop-color="${palette.shellStart}"`);
  derived = derived.replace('stop-color="#7A57E5"', `stop-color="${palette.shellMid}"`);
  derived = derived.replace('stop-color="#5832B9"', `stop-color="${palette.shellEnd}"`);
  derived = derived.replace('stop-color="#30245B"', `stop-color="${palette.visorStart}"`);
  derived = derived.replace('stop-color="#17112E"', `stop-color="${palette.visorEnd}"`);
  derived = derived.replace('stop-color="#ffffff" stop-opacity="0.34"', `stop-color="${palette.highlightStart}" stop-opacity="${palette.highlightStartOpacity}"`);
  derived = derived.replace('stop-color="#ffffff" stop-opacity="0"', `stop-color="${palette.highlightEnd}" stop-opacity="${palette.highlightEndOpacity}"`);
  derived = derived.replaceAll('stroke="#2E1A62"', `stroke="${palette.shellStroke}"`);
  derived = derived.replace('stroke="#8EF0AA"', `stroke="${palette.visorStroke}"`);
  derived = derived.replace('stroke="#AAFFD0"', `stroke="${palette.visorStroke}"`);
  derived = derived.replaceAll('fill="#9AF7A8"', `fill="${palette.detailFill}"`);
  derived = derived.replaceAll('stroke="#9AF7A8"', `stroke="${palette.detailStroke}"`);
  derived = derived.replace('fill="#ffffff" opacity="0.14"', `fill="${palette.sparkleFill}"`);

  // The master viewBox is already tight to content bounds.
  // The menu bar needs a tiny optical inset and downward nudge so the
  // helmet reads centered at 18pt without clipping the crown.

  return derived;
}

const svg = deriveMenubarSvg(masterSvg, tone);

const browser = await chromium.launch({ headless: true });

try {
  const renderScale = 8;
  const renderWidth = width * renderScale;
  const renderHeight = height * renderScale;
  const page = await browser.newPage({
    viewport: { width: renderWidth, height: renderHeight },
    deviceScaleFactor: 1,
  });

  await page.setContent(
    `<!doctype html>
    <html>
      <body style="margin:0; width:${renderWidth}px; height:${renderHeight}px; display:grid; place-items:center; background:transparent; overflow:hidden;">
        <style>
          html, body {
            width: ${renderWidth}px;
            height: ${renderHeight}px;
          }

          .asset-frame {
            width: ${renderWidth}px;
            height: ${renderHeight}px;
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

  const tempDirectory = await mkdtemp(path.join(os.tmpdir(), "longhouse-icon-"));
  const rawOutputPath = path.join(tempDirectory, "raw.png");

  try {
    await page.screenshot({
      path: rawOutputPath,
      omitBackground: true,
    });

    const innerWidth = Math.max(1, Math.round(width * (1 - paddingRatio * 2)));
    const innerHeight = Math.max(1, Math.round(height * (1 - paddingRatio * 2)));
    execFileSync("magick", [
      rawOutputPath,
      "-trim",
      "+repage",
      "-resize",
      `${innerWidth}x${innerHeight}`,
      "-gravity",
      "center",
      "-background",
      "none",
      "-extent",
      `${width}x${height}`,
      path.resolve(outputPath),
    ]);
  } finally {
    await rm(tempDirectory, { recursive: true, force: true });
  }
} finally {
  await browser.close();
}
