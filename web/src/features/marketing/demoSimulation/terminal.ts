import type { GridTimeline } from "@longhouse/video/demo";
import type { DemoSessionEvent } from "./types";

type GridRow = GridTimeline["rowPool"][number];
type GridRun = GridRow[number];

const COLS = 64;
const ROWS = 14;
const CREAM = "#e8e0d2";
const DIM = "#9d958b";
const GREEN = "#9ed8b8";
const GOLD = "#e5c07b";
const RED = "#ef7b70";
const BLUE = "#82b4e8";

function run(text: string, style: Omit<GridRun, "text" | "n"> = {}): GridRun {
  return { text, n: text.length, ...style };
}

function wrapText(text: string, prefix = "", continuation = "  "): string[] {
  const words = text.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [prefix.trimEnd()];
  const lines: string[] = [];
  let line = prefix;
  for (const word of words) {
    const candidate = line.trimEnd().length > prefix.trimEnd().length
      ? `${line} ${word}`
      : `${line}${word}`;
    if (candidate.length <= COLS) {
      line = candidate;
      continue;
    }
    lines.push(line.slice(0, COLS));
    line = `${continuation}${word}`;
  }
  if (line.trim()) lines.push(line.slice(0, COLS));
  return lines;
}

function styledLines(text: string, color: string, prefix = "", continuation = "  ", extra: Partial<GridRun> = {}): GridRow[] {
  return wrapText(text, prefix, continuation).map((line) => [run(line, { fg: color, ...extra })]);
}

function toolResultPreview(output: string): string {
  return output.split("\n").map((line) => line.trim()).filter(Boolean).at(-1) ?? output;
}

function eventRows(event: DemoSessionEvent): GridRow[] {
  switch (event.type) {
    case "instruction_received":
      return styledLines(event.prompt, CREAM, "❯ ", "  ");
    case "assistant_text":
      return [[], ...styledLines(event.text, CREAM, "● ", "  ")];
    case "tool_started":
      return styledLines(event.display, GREEN, `● ${event.tool}(`, "  ").map((row, index, rows) => {
        if (index === rows.length - 1) {
          const last = row[0];
          return [{ ...last, text: `${last.text})`.slice(0, COLS), n: Math.min(COLS, last.n + 1), b: 1 }];
        }
        return row;
      });
    case "tool_result":
      return styledLines(toolResultPreview(event.output), event.failed ? RED : DIM, "  ⎿ ", "    ");
    case "diff_applied": {
      const number = String(event.line).padStart(3, " ");
      const before = `${number} - ${event.before}`.slice(0, COLS);
      const after = `${number} + ${event.after}`.slice(0, COLS);
      return [
        [run(before, { fg: "#ffd0cb", bg: "#5a1714" })],
        [run(after, { fg: "#c7f6d5", bg: "#1e5b20" })],
      ];
    }
    case "test_result":
      return [[run(event.passed ? "✓ tests passed" : "✗ test failed", {
        fg: event.passed ? GREEN : RED,
        b: 1,
      })]];
    case "completed":
      return [[], ...styledLines(event.summary, CREAM, "● ", "  ")];
    case "ready":
      return [
        [run("─".repeat(COLS), { fg: DIM, d: 1 })],
        [run("❯ ", { fg: GOLD, b: 1 })],
        [run("  ⏵⏵ accept edits on · shift+tab to cycle", { fg: BLUE, d: 1 })],
      ];
  }
}

export function renderDemoTerminal(
  prompt: string,
  events: readonly DemoSessionEvent[],
  durationSec: number,
): GridTimeline {
  const rowPool: GridRow[] = [[]];
  const visibleRows: number[] = [];
  const states: GridTimeline["states"] = [{
    t: 0,
    rows: Array.from({ length: ROWS }, () => 0),
    cursor: { x: 2, y: ROWS - 1, visible: true },
    alt: false,
  }];

  for (const event of events) {
    for (const row of eventRows(event)) {
      rowPool.push(row);
      visibleRows.push(rowPool.length - 1);
    }
    const viewport = visibleRows.slice(-ROWS);
    const rows = [...Array.from({ length: ROWS - viewport.length }, () => 0), ...viewport];
    const ready = event.type === "ready";
    states.push({
      t: event.t,
      rows,
      cursor: { x: ready ? 2 : 0, y: ROWS - (ready ? 2 : 1), visible: ready },
      alt: false,
    });
  }

  if (states.at(-1)?.t !== durationSec) {
    states.push({ ...states.at(-1)!, t: durationSec });
  }

  return {
    meta: {
      cols: COLS,
      rows: ROWS,
      prompt,
      promptIdleSec: 0,
      promptTypedSec: 0.2,
    },
    rowPool,
    states,
  };
}
