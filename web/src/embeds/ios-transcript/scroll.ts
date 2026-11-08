// One owner for where the transcript sits, and it is this file.
//
// Native owns the INTENT ("the reader deliberately dragged away from the
// bottom") because only it observes the drag. This side owns the GEOMETRY and
// every correction to it, because only the page can see a row change height
// in the frame it happens:
//
//   pinned    the last row's bottom edge stays at the bottom of the viewport.
//   reading   the row at the top of the viewport stays where it is on screen,
//             whatever changes above it (history arriving, an image loading,
//             a tool opening its output, a keyboard resizing the viewport).
//
// iOS WebKit has no scroll anchoring, so the second is done by hand. Both
// corrections run in the same frame as the change that needs them (resize and
// ResizeObserver callbacks run after layout and before paint), never a frame
// later, so a correction is never visible as a jump.
let stickToBottom = true;

interface Anchor {
  node: Element;
  /**
   * Where `node` sits in the document (top edge plus scroll offset). A reader
   * scrolling does not change this; only layout above the node does, which is
   * exactly the movement to cancel. Comparing viewport positions instead
   * would undo a scroll that has not yet fired its event.
   */
  docY: number;
  /** The next few rows, kept so a vanished anchor has a successor. */
  fallbacks: { node: Element; docY: number }[];
}

let anchor: Anchor | null = null;
/**
 * Points the page has scrolled itself to cancel layout shifts above the reader
 * (never counting a pin or the reader's own scroll). A debug probe reads it:
 * movement of the anchor row in the document minus this is drift the reader saw.
 */
let corrected = 0;
/**
 * Layout shifts the page absorbed, by cause. "render" is a payload changing
 * rows above the reader (history arriving): the old renderer compensated for
 * that case too. "layout" is anything else moving rows above them (an image
 * arriving, a disclosure opening, the viewport resizing): nothing compensated
 * for those before, so this is the movement a reader would have seen.
 */
type ShiftSource = "render" | "layout";
const shiftStats = {
  render: { count: 0, total: 0, max: 0 },
  layout: { count: 0, total: 0, max: 0 },
};

export function scrollStats() {
  const round = (b: { count: number; total: number; max: number }) => ({ count: b.count, total: Math.round(b.total), max: Math.round(b.max) });
  return { corrected, render: round(shiftStats.render), layout: round(shiftStats.layout) };
}
/** Root height at the last ResizeObserver callback; a mismatch means a layout shift is pending. */
let observedHeight = -1;

export function isStickingToBottom(): boolean {
  return stickToBottom;
}

/** `window.setStickToBottom`: native publishes the reader's scroll intent. */
export function setStickToBottom(stick: unknown): void {
  stickToBottom = !!stick;
  if (!stickToBottom) captureAnchor();
}

function root(): Element | null {
  return document.getElementById("root");
}

function maxScroll(): number {
  return Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
}

export function pinToBottom(): void {
  // WebKit can retain an out-of-range DOM scrollY even after UIKit clamps its
  // native offset. Supply the real maximum, not the document height.
  if (window.innerHeight <= 0) return;
  const target = maxScroll();
  // Always write: native's offset can be stale while the DOM already reads the
  // target, and the write is what re-syncs UIScrollView.
  window.scrollTo(0, target);
}

/** Remember which row is at the top of the viewport and where. */
export function captureAnchor(): void {
  const el = root();
  if (!el) return;
  let found: Anchor | null = null;
  const rows: { node: Element; docY: number }[] = [];
  const scrollY = window.scrollY;
  for (const child of el.children) {
    const rect = child.getBoundingClientRect();
    if (rect.bottom <= 0.5) continue;
    rows.push({ node: child, docY: rect.top + scrollY });
    if (rows.length >= 4) break;
  }
  if (rows.length) found = { node: rows[0].node, docY: rows[0].docY, fallbacks: rows.slice(1) };
  anchor = found;
  observedHeight = el.getBoundingClientRect().height;
}

/** A rebuilt row keeps its place: the anchor follows it to its new node. */
export function retargetAnchor(replaced: Map<Element, Element>): void {
  if (!anchor || replaced.size === 0) return;
  const to = (node: Element) => replaced.get(node) ?? node;
  anchor = {
    node: to(anchor.node),
    docY: anchor.docY,
    fallbacks: anchor.fallbacks.map((f) => ({ node: to(f.node), docY: f.docY })),
  };
}

function restoreAnchor(source: ShiftSource): void {
  if (!anchor) return;
  const candidates = [{ node: anchor.node, docY: anchor.docY }, ...anchor.fallbacks];
  const live = candidates.find((c) => c.node.isConnected);
  if (!live) return;
  const delta = live.node.getBoundingClientRect().top + window.scrollY - live.docY;
  if (Math.abs(delta) > 0.5) {
    const before = window.scrollY;
    window.scrollTo(0, before + delta);
    corrected += window.scrollY - before;
    const bucket = shiftStats[source];
    bucket.count += 1;
    bucket.total += Math.abs(delta);
    bucket.max = Math.max(bucket.max, Math.abs(delta));
  }
  captureAnchor();
}

/** Bring the page to where the reader expects it after anything changed. */
export function settle(source: ShiftSource = "layout"): void {
  if (stickToBottom) pinToBottom();
  else restoreAnchor(source);
}

/** Test seam: a fresh document state, as after a document load. */
export function resetScrollForTesting(): void {
  stickToBottom = true;
  anchor = null;
  observedHeight = -1;
  corrected = 0;
  for (const bucket of Object.values(shiftStats)) {
    bucket.count = 0;
    bucket.total = 0;
    bucket.max = 0;
  }
}

export function installScrollOwner(rootElement: Element): void {
  window.addEventListener("resize", () => settle("layout"));
  if (window.visualViewport) window.visualViewport.addEventListener("resize", () => settle("layout"));
  // Track where the reader holds still, unless a layout shift is waiting for
  // its ResizeObserver callback: reading geometry now would bake the shift in.
  window.addEventListener(
    "scroll",
    () => {
      if (stickToBottom) return;
      if (rootElement.getBoundingClientRect().height !== observedHeight) return;
      captureAnchor();
    },
    { passive: true },
  );
  // WebKit always has ResizeObserver; the guard only spares jsdom.
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(() => {
      settle("layout");
      observedHeight = rootElement.getBoundingClientRect().height;
    }).observe(rootElement);
  }
}
