#!/usr/bin/env bun
/**
 * retime.ts — cap inter-event idle gaps in an asciicast v2 file.
 *
 * Timestamps only: never drops, reorders, or edits event data (later cursor
 * ops depend on earlier terminal state). Output timestamps are the cumulative
 * sum of min(gap, MAX_GAP).
 *
 * Usage: bun retime.ts input.cast [output.retimed.cast] [--max-gap-ms 500]
 */

const args = process.argv.slice(2);
if (args.length < 1) {
  console.error("usage: bun retime.ts input.cast [output.cast] [--max-gap-ms 500]");
  process.exit(1);
}
const input = args[0];
let output = input.replace(/\.cast$/, "") + ".retimed.cast";
let maxGapMs = 500;
for (let i = 1; i < args.length; i++) {
  if (args[i] === "--max-gap-ms") maxGapMs = Number(args[++i]);
  else if (!args[i].startsWith("--")) output = args[i];
}
const maxGap = maxGapMs / 1000;

const lines = (await Bun.file(input).text()).split("\n").filter((l) => l.trim());
const out: string[] = [lines[0]]; // header unchanged

let prevT: number | null = null;
let newT = 0;
let capped = 0;
for (let i = 1; i < lines.length; i++) {
  const ev = JSON.parse(lines[i]) as [number, string, string];
  const t = ev[0];
  if (prevT === null) {
    newT = Math.min(t, maxGap); // initial delay also capped
    if (t > maxGap) capped++;
  } else {
    const gap = t - prevT;
    if (gap > maxGap) capped++;
    newT += Math.min(Math.max(gap, 0), maxGap);
  }
  prevT = t;
  out.push(JSON.stringify([Number(newT.toFixed(6)), ev[1], ev[2]]));
}

await Bun.write(output, out.join("\n") + "\n");
const origDur = prevT ?? 0;
console.error(
  `retimed ${lines.length - 1} events: ${origDur.toFixed(1)}s -> ${newT.toFixed(1)}s ` +
    `(${capped} gaps capped at ${maxGapMs}ms) -> ${output}`,
);
