#!/usr/bin/env node

import { chromium } from "playwright";
import { readFile } from "node:fs/promises";
import path from "node:path";

function usage() {
  console.error("Usage: render-svg-asset.mjs <input.svg> <output.png> <width> <height>");
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

const absoluteInput = path.resolve(inputPath);
const svg = await readFile(absoluteInput, "utf8");

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
