import { gzipSync } from 'node:zlib';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';

const distAssetsDir = path.resolve(import.meta.dirname, '../dist/assets');
const budgets = {
  jsTotalGzip: 400 * 1024,
  cssTotalGzip: 60 * 1024,
  totalGzip: 500 * 1024,
  largestJsGzip: 200 * 1024,
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
const largestJs = jsAssets.reduce((largest, asset) => (asset.gzipBytes > largest.gzipBytes ? asset : largest), jsAssets[0]);

console.log('Bundle budget report');
console.log(`- JS total gzip: ${formatKiB(jsTotalGzip)} / ${formatKiB(budgets.jsTotalGzip)}`);
console.log(`- CSS total gzip: ${formatKiB(cssTotalGzip)} / ${formatKiB(budgets.cssTotalGzip)}`);
console.log(`- Total gzip: ${formatKiB(totalGzip)} / ${formatKiB(budgets.totalGzip)}`);
console.log(`- Largest JS gzip: ${largestJs.name} ${formatKiB(largestJs.gzipBytes)} / ${formatKiB(budgets.largestJsGzip)}`);

const failures = [];
if (jsTotalGzip > budgets.jsTotalGzip) failures.push(`JS total gzip exceeded: ${formatKiB(jsTotalGzip)}`);
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
