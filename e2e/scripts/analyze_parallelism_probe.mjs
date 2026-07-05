import fs from "fs";
import path from "path";

const probeDir = path.resolve(process.cwd(), "test-results", "parallelism-probe");

function readJsonl(filePath) {
  const lines = fs.readFileSync(filePath, "utf8").split("\n").filter(Boolean);
  return lines.map((line) => JSON.parse(line));
}

if (!fs.existsSync(probeDir)) {
  console.error(`No probe output found at ${probeDir}`);
  process.exit(1);
}

const files = fs
  .readdirSync(probeDir)
  .filter((f) => f.endsWith(".jsonl"))
  .map((f) => path.join(probeDir, f));

const events = files.flatMap(readJsonl).sort((a, b) => a.t - b.t || (a.type === "end" ? 1 : -1));

const workerSet = new Set(events.map((e) => e.workerIndex));

let active = 0;
let maxActive = 0;
let firstT = null;
let lastT = null;

for (const e of events) {
  if (firstT === null) firstT = e.t;
  lastT = e.t;
  if (e.type === "start") active += 1;
  if (e.type === "end") active -= 1;
  if (active > maxActive) maxActive = active;
}

const durationMs = firstT !== null && lastT !== null ? lastT - firstT : 0;

console.log("Parallelism probe results");
console.log("-------------------------");
console.log(`Workers observed: ${workerSet.size} (${[...workerSet].sort((a, b) => a - b).join(", ")})`);
console.log(`Events: ${events.length}`);
console.log(`Max concurrent tests (from events): ${maxActive}`);
console.log(`Wall time (events span): ${(durationMs / 1000).toFixed(2)}s`);

// Basic heuristic output for quick interpretation
if (workerSet.size <= 1 || maxActive <= 1) {
  console.log("Interpretation: serial (or nearly serial) scheduling detected.");
} else {
  console.log("Interpretation: parallel scheduling detected.");
}
