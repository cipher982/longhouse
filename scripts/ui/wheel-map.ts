/**
 * Wheel map: which element actually scrolls when the mouse wheel turns at each
 * point of the viewport. Ground truth, not CSS inference: for every grid cell
 * it parks every scroll container mid-range, sends a real wheel event through
 * the browser's input pipeline (so scroll chaining and overscroll-behavior are
 * honoured) and records whose scrollTop moved.
 *
 * A "dead" cell is one where the wheel scrolls nothing even though the page has
 * scroll containers. Dead cells over content chrome (gutters, rails, headers
 * that sit beside a scroller) are the bug this probe exists to catch; dead
 * cells over a fixed header/composer are usually intended.
 *
 * Used via `make ui-capture PAGE=<page> SCENE=<scene> WHEEL_MAP=1`.
 */
import type { Page } from "playwright";
import { writeFileSync } from "fs";
import path from "path";

interface ScrollerInfo {
  id: number;
  label: string;
  box: { x: number; y: number; w: number; h: number };
  scrollable: number;
}

interface CellResult {
  x: number;
  y: number;
  owner: number | null;
  under: string;
}

// Evaluated from strings so esbuild's keepNames helper is not injected.
const COLLECT_SCRIPT = `(() => {
  const label = (el) => {
    if (el === document.scrollingElement) return "document";
    const tid = el.getAttribute("data-testid");
    const cls = typeof el.className === "string" ? el.className.trim().split(/\\s+/).slice(0, 2).join(".") : "";
    return el.tagName.toLowerCase() + (tid ? "[" + tid + "]" : "") + (cls ? "." + cls : "");
  };
  const out = [];
  const els = [];
  window.__wmEls = els;
  let id = 0;
  const seen = new Set();
  const consider = (el) => {
    if (seen.has(el)) return;
    seen.add(el);
    const cs = getComputedStyle(el);
    const y = cs.overflowY;
    const isDoc = el === document.scrollingElement;
    if (!isDoc && y !== "auto" && y !== "scroll" && y !== "overlay") return;
    const scrollable = el.scrollHeight - el.clientHeight;
    if (scrollable < 2) return;
    const r = isDoc ? { x: 0, y: 0, width: innerWidth, height: innerHeight } : el.getBoundingClientRect();
    els.push(el);
    out.push({ id, label: label(el), box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }, scrollable });
    id += 1;
  };
  consider(document.scrollingElement);
  for (const el of document.querySelectorAll("*")) consider(el);
  return out;
})()`;

const PARK_SCRIPT = `(() => {
  const els = window.__wmEls;
  for (const el of els) el.scrollTop = Math.max(0, (el.scrollHeight - el.clientHeight) / 2);
  window.__wmSnap = els.map((el) => el.scrollTop);
})()`;

const READ_SCRIPT = (x: number, y: number) => `(() => {
  const els = window.__wmEls;
  const snap = window.__wmSnap;
  const owner = els.findIndex((el, i) => Math.abs(el.scrollTop - snap[i]) > 0.5);
  const hit = document.elementFromPoint(${x}, ${y});
  let under = "";
  if (hit) {
    const tid = hit.closest("[data-testid]")?.getAttribute("data-testid");
    const cls = typeof hit.className === "string" ? hit.className.trim().split(/\\s+/)[0] : "";
    under = hit.tagName.toLowerCase() + (cls ? "." + cls : "") + (tid ? " in [" + tid + "]" : "");
  }
  return { owner: owner < 0 ? null : owner, under };
})()`;

const FRAME_SCRIPT = `(() => {
  const { promise, resolve } = Promise.withResolvers();
  requestAnimationFrame(() => requestAnimationFrame(resolve));
  return promise;
})()`;

const GLYPHS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

export async function captureWheelMap(
  page: Page,
  outputDir: string,
  frameName: string,
  step = 40,
): Promise<void> {
  const scrollers = (await page.evaluate(COLLECT_SCRIPT)) as ScrollerInfo[];
  const vp = page.viewportSize() ?? { width: 1280, height: 720 };
  const cells: CellResult[] = [];
  for (let y = Math.floor(step / 2); y < vp.height; y += step) {
    for (let x = Math.floor(step / 2); x < vp.width; x += step) {
      await page.evaluate(PARK_SCRIPT);
      await page.mouse.move(x, y);
      await page.mouse.wheel(0, 24);
      await page.evaluate(FRAME_SCRIPT);
      const { owner, under } = (await page.evaluate(READ_SCRIPT(x, y))) as {
        owner: number | null;
        under: string;
      };
      cells.push({ x, y, owner, under });
    }
  }
  // Restore: leave scroll positions where a user would find them.
  await page.evaluate("delete window.__wmEls; delete window.__wmSnap");

  const cols = Math.ceil((vp.width - Math.floor(step / 2)) / step);
  const rows: string[] = [];
  for (let i = 0; i < cells.length; i += cols) {
    rows.push(
      cells
        .slice(i, i + cols)
        .map((c) => (c.owner === null ? "." : (GLYPHS[c.owner] ?? "?")))
        .join(""),
    );
  }
  const counts = new Map<string, number>();
  for (const c of cells) {
    if (c.owner === null) counts.set(c.under, (counts.get(c.under) ?? 0) + 1);
  }
  const deadTop = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12);

  const lines: string[] = [];
  lines.push(`Wheel map ${vp.width}x${vp.height}, ${step}px cells ("." = wheel scrolls nothing)`);
  lines.push(...rows);
  lines.push("");
  lines.push("Scrollers:");
  for (const s of scrollers) {
    lines.push(
      `  ${GLYPHS[s.id] ?? "?"} ${s.label} box=${s.box.x},${s.box.y} ${s.box.w}x${s.box.h} scrollable=${Math.round(s.scrollable)}px`,
    );
  }
  lines.push("");
  lines.push(`Dead cells: ${cells.filter((c) => c.owner === null).length}/${cells.length}; most frequent element under a dead cell:`);
  for (const [under, n] of deadTop) lines.push(`  ${n}\t${under}`);

  const text = lines.join("\n");
  const outPath = path.join(outputDir, `${frameName}-wheelmap.txt`);
  writeFileSync(outPath, `${text}\n`);
  writeFileSync(
    path.join(outputDir, `${frameName}-wheelmap.json`),
    JSON.stringify({ viewport: vp, step, scrollers, cells }, null, 2),
  );
  console.log(text);
  console.log(`  Wheel map: ${outPath}`);
}
