/**
 * Stage geometry for the hero story, computed from the measured stage width.
 *
 * Absolute rects (not flow layout) because the story MOVES things between
 * layouts: terminals dock into timeline rows and a row grows back into a
 * terminal. Interpolating two known rects is the whole trick.
 */

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Terminal chrome + body padding + borders around the recorded grid. */
const TERMINAL_CHROME_PX = 51;
/** 64x14 recordings at CELL_RATIO 2.15 (see ResponsiveTerminal). */
const GRID_ASPECT = (14 * 2.15) / 64;

export const terminalHeight = (width: number) => width * GRID_ASPECT + TERMINAL_CHROME_PX;

export interface HeroLayout {
  narrow: boolean;
  width: number;
  height: number;
  /** Agents chapter: one rect per session. */
  deck: Rect[];
  panel: Rect;
  panelHeaderH: number;
  rowH: number;
  /** Where each docking terminal lands: the row's leading slot. */
  thumb: (index: number) => Rect;
  terminal: Rect;
  phone: Rect;
}

export function heroLayout(width: number): HeroLayout {
  const narrow = width < 520;

  const tileW = narrow ? width : Math.round(width * 0.78);
  const tileH = terminalHeight(tileW);
  const deckStep = narrow ? tileH * 0.6 : tileH * 0.64;
  const deck: Rect[] = narrow
    ? [0, 1, 2].map((i) => ({ x: 0, y: i * deckStep, w: tileW, h: tileH }))
    : [
        { x: 0, y: 0, w: tileW, h: tileH },
        { x: width - tileW, y: deckStep, w: tileW, h: tileH },
        { x: (width - tileW) / 2, y: deckStep * 2, w: tileW, h: tileH },
      ];
  const deckH = deckStep * 2 + tileH;

  let terminal: Rect;
  let phone: Rect;
  let height: number;
  if (narrow) {
    const phoneH = Math.max(270, width * 0.78);
    terminal = { x: 0, y: 0, w: width, h: tileH };
    height = Math.max(deckH, tileH + 14 + phoneH);
    phone = { x: width * 0.04, y: tileH + 14, w: width * 0.92, h: height - tileH - 14 };
  } else {
    // Diagonal: terminal upper-right in front, phone lower-left tucked under
    // its corner, where the phone shows only status bar and nav.
    height = deckH;
    const termW = Math.round(width * 0.7);
    const phoneW = Math.round(width * 0.39);
    const phoneH = Math.min(height, phoneW * 2.05);
    terminal = { x: width - termW, y: height * 0.04, w: termW, h: terminalHeight(termW) };
    phone = { x: 0, y: height - phoneH, w: phoneW, h: phoneH };
  }

  const panelHeaderH = narrow ? 50 : 108;
  const rowH = narrow ? 66 : 70;
  const panelH = panelHeaderH + rowH * 3 + 8;
  const panel = { x: 0, y: Math.max(0, (height - panelH) * 0.5), w: width, h: panelH };

  const thumb = (index: number): Rect => {
    const h = rowH - 22;
    const w = h / GRID_ASPECT;
    return {
      x: 14,
      y: panel.y + panelHeaderH + index * rowH + (rowH - h) / 2,
      w,
      h,
    };
  };

  return { narrow, width, height, deck, panel, panelHeaderH, rowH, thumb, terminal, phone };
}

export const lerp = (a: number, b: number, p: number) => a + (b - a) * p;

/**
 * CSS transform that draws an element whose laid-out box is `natural` at a
 * point between rects `a` and `b`: position interpolates, and the uniform
 * scale interpolates by height, anchored top-left.
 */
export function placeTransform(natural: Rect, a: Rect, b: Rect, p: number): string {
  const scale = lerp(a.h, b.h, p) / natural.h;
  const x = lerp(a.x, b.x, p);
  const y = lerp(a.y, b.y, p);
  return `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px) scale(${scale.toFixed(4)})`;
}
