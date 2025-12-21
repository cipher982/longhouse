import fs from "fs";
import path from "path";

function usage() {
  console.log("Usage: node scripts/analyze_playwright_timeline.mjs [path-to-json]");
  process.exit(2);
}

const argPath = process.argv[2];
const jsonPath = argPath ? path.resolve(process.cwd(), argPath) : path.resolve(process.cwd(), "test-results/playwright-timeline.json");

if (!fs.existsSync(jsonPath)) {
  console.error(`Timeline JSON not found: ${jsonPath}`);
  usage();
}

const report = JSON.parse(fs.readFileSync(jsonPath, "utf8"));

function walkSuites(suites, out) {
  for (const suite of suites ?? []) {
    for (const spec of suite.specs ?? []) {
      for (const test of spec.tests ?? []) {
        for (const result of test.results ?? []) {
          out.push({ test, result, spec, suite });
        }
      }
    }
    walkSuites(suite.suites, out);
  }
}

const items = [];
walkSuites(report.suites, items);

const intervals = items
  .map(({ test, result }) => {
    const startMs = new Date(result.startTime).getTime();
    const endMs = startMs + (result.duration ?? 0);
    return {
      title: test.title,
      workerIndex: result.workerIndex,
      status: result.status,
      startMs,
      endMs,
      durationMs: result.duration ?? 0,
    };
  })
  .filter((i) => Number.isFinite(i.startMs) && Number.isFinite(i.endMs));

if (intervals.length === 0) {
  console.error("No test results found in timeline JSON.");
  process.exit(1);
}

const workerSet = new Set(intervals.map((i) => i.workerIndex));
const startMin = Math.min(...intervals.map((i) => i.startMs));
const endMax = Math.max(...intervals.map((i) => i.endMs));

const events = [];
for (const i of intervals) {
  events.push({ t: i.startMs, delta: +1 });
  events.push({ t: i.endMs, delta: -1 });
}
events.sort((a, b) => a.t - b.t || b.delta - a.delta);

let active = 0;
let maxActive = 0;
for (const e of events) {
  active += e.delta;
  if (active > maxActive) maxActive = active;
}

const wallMs = endMax - startMin;

console.log("Playwright timeline summary");
console.log("--------------------------");
console.log(`File: ${path.relative(process.cwd(), jsonPath)}`);
console.log(`Tests (results): ${intervals.length}`);
console.log(`Workers observed: ${workerSet.size} (${[...workerSet].sort((a, b) => a - b).join(", ")})`);
console.log(`Max concurrent tests: ${maxActive}`);
console.log(`Wall time (min start → max end): ${(wallMs / 1000).toFixed(2)}s`);

const passed = intervals.filter((i) => i.status === "passed").length;
const failed = intervals.filter((i) => i.status === "failed").length;
const skipped = intervals.filter((i) => i.status === "skipped").length;
console.log(`Status: passed=${passed} failed=${failed} skipped=${skipped}`);

if (workerSet.size <= 1 || maxActive <= 1) {
  console.log("Interpretation: serial (or nearly serial) execution.");
} else {
  console.log("Interpretation: parallel execution.");
}
