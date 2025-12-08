import { test, expect } from './fixtures';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { PNG } from 'pngjs';
import pixelmatch from 'pixelmatch';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BASELINE_PATH = path.resolve(__dirname, '../visual-baseline/dashboard-legacy.png');
const DIFF_PATH = path.resolve(__dirname, '../visual-baseline/dashboard-legacy-diff.png');

function readPng(buffer: Buffer): PNG {
  return PNG.sync.read(buffer);
}

function writePng(png: PNG, outputPath: string) {
  fs.writeFileSync(outputPath, PNG.sync.write(png));
}

test('React dashboard matches legacy visual baseline', async ({ page }) => {
  await page.goto('/dashboard');

  await page.waitForSelector('#agents-table', { timeout: 15000 });

  const screenshot = await page.locator('#dashboard-container').screenshot({
    animations: 'disabled',
    scale: 'device',
  });

  if (!fs.existsSync(BASELINE_PATH)) {
    throw new Error(
      `Dashboard baseline missing at ${BASELINE_PATH}. Capture legacy Rust screenshot and place it at this path before running parity tests.`
    );
  }

  const baseline = readPng(fs.readFileSync(BASELINE_PATH));
  const current = readPng(screenshot);

  if (baseline.width !== current.width || baseline.height !== current.height) {
    throw new Error(
      `Screenshot dimensions changed (baseline ${baseline.width}x${baseline.height}, current ${current.width}x${current.height}). Update the baseline if this is expected.`
    );
  }

  const diff = new PNG({ width: baseline.width, height: baseline.height });
  const diffPixelCount = pixelmatch(
    baseline.data,
    current.data,
    diff.data,
    baseline.width,
    baseline.height,
    { threshold: 0.05 }
  );

  const totalPixels = baseline.width * baseline.height;
  const diffRatio = diffPixelCount / totalPixels;

  if (diffPixelCount > 0) {
    writePng(diff, DIFF_PATH);
  }

  expect(diffRatio).toBeLessThan(0.001);
});
