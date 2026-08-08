#!/usr/bin/env bun
/**
 * render-grid-html.ts — render one state of a compiled grid timeline to HTML.
 *
 * Spike-quality renderer for fidelity comparison against agg. Colors use the
 * standard xterm 256 palette; final Remotion styling will differ.
 *
 * Usage: bun render-grid-html.ts timeline.grid.json --t 12.3 --out state.html
 *        bun render-grid-html.ts timeline.grid.json --index 5 --out state.html
 */

type Run = {
  text: string; n: number;
  fg?: number | string; bg?: number | string;
  b?: 1; d?: 1; i?: 1; u?: 1; inv?: 1;
};

const args = process.argv.slice(2);
const input = args[0];
let tArg: number | null = null;
let idxArg: number | null = null;
let out = "state.html";
for (let i = 1; i < args.length; i++) {
  if (args[i] === "--t") tArg = Number(args[++i]);
  else if (args[i] === "--index") idxArg = Number(args[++i]);
  else if (args[i] === "--out") out = args[++i];
}

const doc = JSON.parse(await Bun.file(input).text());
const { cols, rows } = doc.meta;
const states: { t: number; rows: number[]; cursor: { x: number; y: number; visible: boolean } }[] =
  doc.states;

let state = states[states.length - 1];
let stateIdx = states.length - 1;
if (idxArg !== null) {
  stateIdx = idxArg;
  state = states[idxArg];
} else if (tArg !== null) {
  // latest state at-or-before t — same rule the video will use per frame
  stateIdx = 0;
  for (let i = 0; i < states.length; i++) {
    if (states[i].t <= tArg) stateIdx = i;
    else break;
  }
  state = states[stateIdx];
}

// Standard xterm-256 palette.
const BASE16 = [
  "#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
  "#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff",
];
const palette = (n: number): string => {
  if (n < 16) return BASE16[n];
  if (n < 232) {
    const v = n - 16;
    const c = [Math.floor(v / 36) % 6, Math.floor(v / 6) % 6, v % 6].map((x) =>
      x === 0 ? 0 : 55 + x * 40,
    );
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  }
  const g = 8 + (n - 232) * 10;
  return `rgb(${g},${g},${g})`;
};
const color = (c: number | string | undefined, fallback: string): string =>
  c === undefined ? fallback : typeof c === "string" ? c : palette(c);

const DEFAULT_FG = "#e5e5e5";
const DEFAULT_BG = "#121212";

const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

const renderRun = (r: Run): string => {
  let fg = color(r.fg, DEFAULT_FG);
  let bg = color(r.bg, DEFAULT_BG);
  if (r.b && typeof r.fg === "number" && r.fg < 8) fg = BASE16[r.fg + 8]; // bold brightens low palette
  if (r.inv) [fg, bg] = [bg, fg];
  const styles = [
    `color:${fg}`,
    bg !== DEFAULT_BG ? `background:${bg}` : "",
    r.b ? "font-weight:700" : "",
    r.d ? "opacity:0.6" : "",
    r.i ? "font-style:italic" : "",
    r.u ? "text-decoration:underline" : "",
  ].filter(Boolean).join(";");
  return `<span style="${styles}">${esc(r.text)}</span>`;
};

const rowsHtml = state.rows
  .map((poolIdx: number, y: number) => {
    const runs: Run[] = doc.rowPool[poolIdx];
    let html = runs.map(renderRun).join("");
    const used = runs.reduce((a, r) => a + r.n, 0);
    if (used < cols) html += `<span>${" ".repeat(cols - used)}</span>`;
    if (state.cursor.visible && state.cursor.y === y) {
      html += `<span class="cursor" style="left:${state.cursor.x}ch"></span>`;
    }
    return `<div class="row">${html}</div>`;
  })
  .join("\n");

const html = `<!doctype html>
<meta charset="utf-8">
<title>grid state ${stateIdx} t=${state.t}</title>
<style>
  body { margin: 0; background: ${DEFAULT_BG}; }
  .term {
    font: 400 16px/1.25 "Menlo", "SF Mono", monospace;
    color: ${DEFAULT_FG};
    white-space: pre;
    padding: 12px;
    display: inline-block;
  }
  .row { position: relative; height: 1.25em; }
  .cursor {
    position: absolute; top: 0; width: 1ch; height: 1.25em;
    background: rgba(229,229,229,0.55);
  }
</style>
<div class="term">
${rowsHtml}
</div>`;

await Bun.write(out, html);
console.error(`state ${stateIdx} (t=${state.t}s) -> ${out}`);
