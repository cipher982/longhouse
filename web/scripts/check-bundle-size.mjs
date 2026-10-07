import { gzipSync } from 'node:zlib';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';

const distDir = path.resolve(import.meta.dirname, '../dist');
const distAssetsDir = path.join(distDir, 'assets');

// Budgets are gzip sizes of dist/assets as this script measures them in CI
// (Node 20; Node 26 reads 0.3% lower on the same files). When one trips,
// diff per-chunk gzip against the last green run's web-dist artifact before
// touching a number: a limit that was hit is evidence, not a knob.
//
// jsTotalGzip sums every JS chunk, including pages that load on demand and are
// never downloaded together. It is a coarse tripwire for a heavy dependency,
// not what a visitor pays. Measured 2026-09-30: 451.5 KiB in CI, up 23.1 KiB
// from 427.1 on 2026-09-25 (web: lazy-load app-shell pages). That growth is
// feature code in lazy chunks: the Hearth fire renderer and its timeline wiring
// +16.6, session detail +3.3, the rest spread over the other app pages; the
// docs and privacy copy that tipped it over is +1.7 in the entry chunk. No
// dependency was added, and prerender code (react-dom/static) is in no client
// chunk. Budget = 451.5 + 16.6 (the largest single feature since the split) =
// 468.1, rounded up to 470.
// Re-measured 2026-10-07: 471.8 KiB in CI (first over at 470.4 with the host
// update continuity web work, a5a130e9e). Per-chunk gzip against the last green
// run's web-dist (a77248039, 2026-10-06): +4.1 KiB, all feature code in existing
// chunks: base +1.8 (host-update continuity), session detail +0.8 (steer and
// attachments), ModelPicker +0.8 (composer model chip), index +0.5. No
// dependency changed (web/package.json identical). Same margin rule:
// 471.8 + 16.6 = 488.4, rounded up to 490.
//
// entryGzip is what index.html loads before any route renders (the entry
// script and its modulepreloads): the cost every visitor, landing page
// included, pays. The public pages live in it so their prerendered HTML can
// hydrate. Measured 2026-09-30: 147.4 KiB locally, about 147.8 in CI, up 1.1
// from 146.3 on 2026-09-25. Budget = 147.8 + 12 = 159.8, rounded up to 160:
// room for a handful of new docs pages (about 2.5 KiB each) but not for
// react-markdown (about 30) or dnd-kit (about 28) to join the entry.
const budgets = {
  jsTotalGzip: 490 * 1024,
  entryGzip: 160 * 1024,
  cssTotalGzip: 70 * 1024,
  totalGzip: 550 * 1024,
  largestJsGzip: 280 * 1024,
};

function formatKiB(bytes) {
  return `${(bytes / 1024).toFixed(1)} KiB`;
}

function readAssets(extension) {
  return readdirSync(distAssetsDir)
    .filter((name) => name.endsWith(extension))
    .map((name) => {
      const filePath = path.join(distAssetsDir, name);
      const raw = readFileSync(filePath);
      return {
        name,
        rawBytes: statSync(filePath).size,
        gzipBytes: gzipSync(raw).length,
      };
    });
}

const jsAssets = readAssets('.js');
const cssAssets = readAssets('.css');

if (jsAssets.length === 0) {
  throw new Error(`No JS assets found in ${distAssetsDir}; run the frontend build first.`);
}

const jsTotalGzip = jsAssets.reduce((sum, asset) => sum + asset.gzipBytes, 0);
const cssTotalGzip = cssAssets.reduce((sum, asset) => sum + asset.gzipBytes, 0);
const totalGzip = jsTotalGzip + cssTotalGzip;

// A pattern that stops matching must fail the check, not pass it on zero chunks.
const entryNames = new Set(
  [...readFileSync(path.join(distDir, 'index.html'), 'utf8').matchAll(/(?:src|href)="\/assets\/([^"]+\.js)"/g)].map((match) => match[1]),
);
const entryAssets = jsAssets.filter((asset) => entryNames.has(asset.name));
if (entryNames.size === 0 || entryAssets.length !== entryNames.size) {
  throw new Error(`dist/index.html names ${entryNames.size} JS assets but ${entryAssets.length} are in ${distAssetsDir}.`);
}
const entryGzip = entryAssets.reduce((sum, asset) => sum + asset.gzipBytes, 0);
const largestJs = jsAssets.reduce((largest, asset) => (asset.gzipBytes > largest.gzipBytes ? asset : largest), jsAssets[0]);

console.log('Bundle budget report');
console.log(`- JS total gzip: ${formatKiB(jsTotalGzip)} / ${formatKiB(budgets.jsTotalGzip)}`);
console.log(`- Entry JS gzip (index.html): ${formatKiB(entryGzip)} / ${formatKiB(budgets.entryGzip)}`);
console.log(`- CSS total gzip: ${formatKiB(cssTotalGzip)} / ${formatKiB(budgets.cssTotalGzip)}`);
console.log(`- Total gzip: ${formatKiB(totalGzip)} / ${formatKiB(budgets.totalGzip)}`);
console.log(`- Largest JS gzip: ${largestJs.name} ${formatKiB(largestJs.gzipBytes)} / ${formatKiB(budgets.largestJsGzip)}`);

const failures = [];
if (jsTotalGzip > budgets.jsTotalGzip) failures.push(`JS total gzip exceeded: ${formatKiB(jsTotalGzip)}`);
if (entryGzip > budgets.entryGzip) failures.push(`Entry JS gzip exceeded: ${formatKiB(entryGzip)}`);
if (cssTotalGzip > budgets.cssTotalGzip) failures.push(`CSS total gzip exceeded: ${formatKiB(cssTotalGzip)}`);
if (totalGzip > budgets.totalGzip) failures.push(`Combined gzip exceeded: ${formatKiB(totalGzip)}`);
if (largestJs.gzipBytes > budgets.largestJsGzip) failures.push(`Largest JS chunk exceeded: ${largestJs.name} ${formatKiB(largestJs.gzipBytes)}`);

if (failures.length > 0) {
  console.error('\nBundle budgets failed:');
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

console.log('\nBundle budgets passed.');
