#!/usr/bin/env bun
/**
 * compile.ts — asciicast v2 -> styled grid timeline JSON, offline.
 *
 * Replays the cast through @xterm/headless (awaiting EVERY write), and after
 * each output event serializes the ACTIVE buffer viewport into rows of styled
 * runs. A state is only emitted when the visible grid (or cursor) changed.
 * Identical rows are deduped through a row pool: states store indices into
 * meta-level `rowPool`.
 *
 * Output shape:
 * {
 *   meta: { cols, rows, source, sha256, generatedAt, events, statesEmitted },
 *   rowPool: [ [run, run, ...], ... ],          // run = {text,n,fg?,bg?,b?,d?,i?,u?,inv?}
 *   states: [ { t, rows: [poolIdx x rows], cursor: {x,y,visible} }, ... ]
 * }
 * fg/bg: absent = default; number = 16/256 palette index; "#rrggbb" = truecolor.
 * n = columns spanned (wide glyphs span 2; width-0 continuation cells are skipped).
 *
 * Usage: bun compile.ts input.cast [output.grid.json]
 */

import { createHash } from "node:crypto";
import { basename } from "node:path";
// @ts-expect-error no bundled types resolution issue under bun is fine for the spike
import xterm from "@xterm/headless";

const { Terminal } = xterm as unknown as {
  Terminal: new (opts: Record<string, unknown>) => any;
};

const input = process.argv[2];
if (!input) {
  console.error("usage: bun compile.ts input.cast [output.grid.json]");
  process.exit(1);
}
const output = process.argv[3] ?? input.replace(/\.cast$/, "") + ".grid.json";

const raw = await Bun.file(input).text();
const lines = raw.split("\n").filter((l) => l.trim());
const header = JSON.parse(lines[0]);
let cols: number = header.width;
let rows: number = header.height;

const term = new Terminal({
  cols,
  rows,
  scrollback: 1000,
  allowProposedApi: true,
  // Claude Code emits truecolor + 256; defaults handle both.
});

const write = (data: string) =>
  new Promise<void>((resolve) => term.write(data, resolve));

// DECTCEM cursor visibility tracked from the raw stream (xterm.js does not
// expose it publicly). Last occurrence in the stream wins.
let cursorVisible = true;
const trackCursorVisibility = (data: string) => {
  const l = data.lastIndexOf("\x1b[?25l");
  const h = data.lastIndexOf("\x1b[?25h");
  if (l > h) cursorVisible = false;
  else if (h > l) cursorVisible = true;
};

type Run = {
  text: string;
  n: number; // columns spanned
  fg?: number | string;
  bg?: number | string;
  b?: 1; d?: 1; i?: 1; u?: 1; inv?: 1;
};

const colorOf = (cell: any, which: "Fg" | "Bg"): number | string | undefined => {
  if (cell[`is${which}Default`]()) return undefined;
  const c = cell[`get${which}Color`]();
  if (cell[`is${which}RGB`]()) {
    return "#" + (c >>> 0).toString(16).padStart(6, "0");
  }
  return c; // palette index (16 or 256 color)
};

const serializeRow = (line: any): Run[] => {
  const runs: Run[] = [];
  let cur: Run | null = null;
  for (let x = 0; x < cols; x++) {
    const c = line?.getCell(x);
    if (!c) break;
    const width = c.getWidth();
    if (width === 0) continue; // continuation of a wide glyph
    const text = c.getChars() || " ";
    const style = {
      fg: colorOf(c, "Fg"),
      bg: colorOf(c, "Bg"),
      b: c.isBold() ? (1 as const) : undefined,
      d: c.isDim() ? (1 as const) : undefined,
      i: c.isItalic() ? (1 as const) : undefined,
      u: c.isUnderline() ? (1 as const) : undefined,
      inv: c.isInverse() ? (1 as const) : undefined,
    };
    const sameStyle =
      cur &&
      cur.fg === style.fg && cur.bg === style.bg &&
      cur.b === style.b && cur.d === style.d && cur.i === style.i &&
      cur.u === style.u && cur.inv === style.inv;
    if (sameStyle && cur) {
      cur.text += text;
      cur.n += width;
    } else {
      cur = { text, n: width, ...style };
      // strip undefined keys for compact JSON
      for (const k of Object.keys(cur) as (keyof Run)[]) {
        if (cur[k] === undefined) delete cur[k];
      }
      runs.push(cur);
    }
  }
  // Trim trailing default-styled spaces to save space (renderer pads rows).
  const last = runs[runs.length - 1];
  if (last && !last.fg && !last.bg && !last.b && !last.d && !last.i && !last.u && !last.inv) {
    const stripped = last.text.replace(/ +$/, "");
    last.n -= last.text.length - stripped.length; // spaces are width-1
    last.text = stripped;
    if (last.text.length === 0) runs.pop();
  }
  return runs;
};

// Row pool: serialized row JSON -> pool index.
const rowPool: Run[][] = [];
const rowIndex = new Map<string, number>();
const poolRow = (runs: Run[]): number => {
  const key = JSON.stringify(runs);
  let idx = rowIndex.get(key);
  if (idx === undefined) {
    idx = rowPool.length;
    rowPool.push(runs);
    rowIndex.set(key, idx);
  }
  return idx;
};

type State = {
  t: number;
  rows: number[];
  cursor: { x: number; y: number; visible: boolean };
  alt: boolean;
};
const states: State[] = [];
let lastHash = "";

const snapshot = (t: number) => {
  const buf = term.buffer.active;
  const top = buf.baseY ?? 0; // bottom page of the buffer = visible viewport
  const rowIdxs: number[] = [];
  for (let y = 0; y < rows; y++) {
    rowIdxs.push(poolRow(serializeRow(buf.getLine(top + y))));
  }
  const cursor = {
    x: buf.cursorX,
    y: buf.cursorY,
    visible: cursorVisible,
  };
  const alt = buf.type === "alternate";
  const hash = createHash("sha1")
    .update(rowIdxs.join(",") + "|" + JSON.stringify(cursor) + "|" + alt)
    .digest("hex");
  if (hash === lastHash) return;
  lastHash = hash;
  states.push({ t: Number(t.toFixed(3)), rows: rowIdxs, cursor, alt });
};

let events = 0;
for (let i = 1; i < lines.length; i++) {
  const ev = JSON.parse(lines[i]) as [number, string, string];
  const [t, kind, data] = ev;
  if (kind === "o") {
    events++;
    trackCursorVisibility(data);
    await write(data);
    snapshot(t);
  } else if (kind === "r") {
    const [c, r] = String(data).split("x").map(Number);
    cols = c;
    rows = r;
    term.resize(c, r);
    snapshot(t);
  }
  // "i" (input) and markers are ignored for rendering.
}

const sha256 = createHash("sha256").update(raw).digest("hex");
// Deterministic output: same cast in, byte-identical grid out. The cast's
// sha256 is the provenance link; no absolute paths or wall-clock stamps.
const doc = {
  meta: {
    cols,
    rows,
    source: basename(input),
    sha256,
    events,
    statesEmitted: states.length,
    rowPoolSize: rowPool.length,
  },
  rowPool,
  states,
};
const json = JSON.stringify(doc);
await Bun.write(output, json);
console.error(
  `compiled ${events} output events -> ${states.length} states, ` +
    `${rowPool.length} pooled rows, ${(json.length / 1024).toFixed(1)} KiB -> ${output}`,
);
term.dispose();
