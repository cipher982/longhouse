import React, { useMemo } from "react";

/**
 * Pure renderer for compiled real-PTY grid timelines
 * (video/src/assets/terminal/*.grid.json, produced by scripts/terminal/compile.ts).
 *
 * No emulator runs here: given a time offset, binary-search the latest state
 * and draw its styled runs on an exact cell lattice. Rows and runs are
 * absolutely positioned in cell units so box-drawing and block glyphs tile
 * seamlessly regardless of font metrics (the V1 spike's line-height seams).
 */

interface Run {
  text: string;
  n: number;
  fg?: number | string;
  bg?: number | string;
  b?: 1;
  d?: 1;
  i?: 1;
  u?: 1;
  inv?: 1;
}

interface GridState {
  t: number;
  rows: number[];
  cursor: { x: number; y: number; visible: boolean };
  alt: boolean;
}

export interface GridTimeline {
  meta: { cols: number; rows: number };
  rowPool: Run[][];
  states: GridState[];
}

const DEFAULT_FG = "#e8e0d2";

/** xterm 256-color palette; 0-15 tuned to sit well on the warm dark chrome. */
const ANSI16 = [
  "#141414", "#e06c60", "#8fc5ac", "#e5c07b",
  "#6fa7dc", "#c586c0", "#56b6c2", "#d8d3c8",
  "#5c554d", "#ef7b70", "#a3d9be", "#f0d197",
  "#82b4e8", "#d69ad1", "#6cc7d4", "#f3ead9",
];

function xterm256(index: number): string {
  if (index < 16) return ANSI16[index];
  if (index < 232) {
    const v = index - 16;
    const steps = [0, 95, 135, 175, 215, 255];
    const r = steps[Math.floor(v / 36) % 6];
    const g = steps[Math.floor(v / 6) % 6];
    const b = steps[v % 6];
    return `rgb(${r},${g},${b})`;
  }
  const gray = 8 + (index - 232) * 10;
  return `rgb(${gray},${gray},${gray})`;
}

function color(value: number | string | undefined, fallback: string): string {
  if (value === undefined) return fallback;
  if (typeof value === "string") return value;
  return xterm256(value);
}

/** Latest state at or before t (binary search). */
function stateAt(states: GridState[], t: number): GridState {
  let lo = 0;
  let hi = states.length - 1;
  if (t <= states[0].t) return states[0];
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (states[mid].t <= t) lo = mid;
    else hi = mid - 1;
  }
  return states[lo];
}

export const TerminalGrid: React.FC<{
  timeline: GridTimeline;
  /** Seconds into the recording to display. */
  tSec: number;
  /** Exact cell box; font size derives from cellH. */
  cellW: number;
  cellH: number;
  fontFamily?: string;
  background?: string;
}> = ({ timeline, tSec, cellW, cellH, fontFamily, background = "transparent" }) => {
  const { cols, rows } = timeline.meta;
  const state = useMemo(
    () => stateAt(timeline.states, tSec),
    [timeline, tSec],
  );

  return (
    <div
      style={{
        position: "relative",
        width: cols * cellW,
        height: rows * cellH,
        background,
        fontFamily:
          fontFamily ?? '"JetBrains Mono", "SF Mono", Menlo, Consolas, monospace',
        // Lock font size to BOTH cell axes: the monospace advance (~0.62em)
        // must not exceed cellW, or 64-100 column rows drift horizontally.
        fontSize: Math.min(cellH * 0.72, cellW / 0.62),
        lineHeight: `${cellH}px`,
        fontVariantLigatures: "none",
        letterSpacing: 0,
        overflow: "hidden",
      }}
    >
      {state.rows.map((rowIndex, y) => {
        const runs = timeline.rowPool[rowIndex];
        if (!runs || runs.length === 0) return null;
        let x = 0;
        return (
          <div key={y} style={{ position: "absolute", top: y * cellH, left: 0, height: cellH }}>
            {runs.map((run, i) => {
              const left = x * cellW;
              x += run.n;
              const isBlank = run.text.trim() === "" && !run.bg && !run.inv;
              if (isBlank) return null;
              let fg = color(run.fg, DEFAULT_FG);
              let bg = color(run.bg, "transparent");
              if (run.inv) [fg, bg] = [bg === "transparent" ? "#151009" : bg, fg];
              return (
                <span
                  key={i}
                  style={{
                    position: "absolute",
                    left,
                    width: run.n * cellW,
                    height: cellH,
                    color: fg,
                    background: bg === "transparent" ? undefined : bg,
                    fontWeight: run.b ? 700 : 400,
                    opacity: run.d ? 0.55 : 1,
                    fontStyle: run.i ? "italic" : undefined,
                    textDecoration: run.u ? "underline" : undefined,
                    whiteSpace: "pre",
                    overflow: "hidden",
                  }}
                >
                  {run.text}
                </span>
              );
            })}
          </div>
        );
      })}
      {state.cursor.visible ? (
        <div
          style={{
            position: "absolute",
            left: state.cursor.x * cellW,
            top: state.cursor.y * cellH,
            width: cellW,
            height: cellH,
            background: "rgba(201, 166, 107, 0.85)",
          }}
        />
      ) : null}
    </div>
  );
};
