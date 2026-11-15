// Keyed, in-place DOM update. A row's nodes are kept for as long as its item's
// JSON is unchanged, so an update that changes one row (a streaming message, a
// tool getting its output) leaves every other row's nodes, decoded images,
// scroll positions and text selection alone. An item can render to several
// top-level nodes (a tool row and its failure preview and worker list), so
// each entry tracks all of them.
import { escapeHtml } from "./escape";
import { renderItem } from "./rows";
import type { TranscriptItem } from "./types";

interface Entry {
  signature: string;
  nodes: Element[];
}

const STATE_KEY = "\u0000state";
const ERROR_KEY = "\u0000error";

let entries = new Map<string, Entry>();

export function resetReconciler(): void {
  entries = new Map();
}

export interface ReconcileResult {
  html_ms: number;
  dom_ms: number;
  /** For each item whose nodes were rebuilt, the node it used to start with and the one that replaced it. */
  replaced: Map<Element, Element>;
}

export type Handlers = (scope: ParentNode) => void;

/** What a rebuilt row should look like to the reader: what they opened and scrolled. */
interface UiState {
  open: Set<string>;
  scrollLeft: number[];
}

const SCROLLERS = "pre, .table-wrap";

function captureUi(nodes: Element[]): UiState {
  const open = new Set<string>();
  const scrollLeft: number[] = [];
  for (const node of nodes) {
    if (node.matches("details[data-open-key][open]")) open.add(node.getAttribute("data-open-key")!);
    for (const d of node.querySelectorAll("details[data-open-key][open]")) open.add(d.getAttribute("data-open-key")!);
    for (const el of node.matches(SCROLLERS) ? [node] : []) scrollLeft.push((el as HTMLElement).scrollLeft);
    for (const el of node.querySelectorAll<HTMLElement>(SCROLLERS)) scrollLeft.push(el.scrollLeft);
  }
  return { open, scrollLeft };
}

/** Needs the nodes attached: scroll offsets mean nothing without layout. */
function applyUi(nodes: Element[], state: UiState): void {
  let index = 0;
  for (const node of nodes) {
    const details = [...(node.matches("details[data-open-key]") ? [node] : []), ...node.querySelectorAll("details[data-open-key]")];
    for (const d of details) if (state.open.has(d.getAttribute("data-open-key") ?? "")) (d as HTMLDetailsElement).open = true;
    const scrollers = [...(node.matches(SCROLLERS) ? [node] : []), ...node.querySelectorAll<HTMLElement>(SCROLLERS)] as HTMLElement[];
    for (const el of scrollers) {
      const left = state.scrollLeft[index++];
      if (left) el.scrollLeft = left;
    }
  }
}

function updateIndices(nodes: Element[], index: number): void {
  for (const node of nodes) {
    if (node.hasAttribute("data-message-index")) node.setAttribute("data-message-index", String(index));
    for (const button of node.querySelectorAll("[data-expand-index]")) button.setAttribute("data-expand-index", String(index));
  }
}

function build(html: string, attach: Handlers): Element[] {
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  attach(template.content);
  return Array.from(template.content.children);
}

function isRecoveredApiError(items: TranscriptItem[], index: number): boolean {
  const item = items[index];
  if (!item?.apiError) return false;
  for (let j = items.length - 1; j > index; j--) {
    const later = items[j];
    if (later.kind === "tool" || later.kind === "activityGroup" || later.kind === "compaction") return true;
    if (later.kind === "message" && (!later.apiError || Boolean(later.body?.trim()))) return true;
  }
  return false;
}

/**
 * Make `root` show `items` (or the empty/error document) with the fewest DOM
 * operations. Nodes already in the right place are not touched.
 */
export function reconcile(
  root: Element,
  items: TranscriptItem[],
  errorMessage: string | null | undefined,
  attach: Handlers,
): ReconcileResult {
  let htmlMs = 0;
  let domMs = 0;
  const replaced = new Map<Element, Element>();
  const desired: { key: string; entry: Entry; reused: boolean; index: number }[] = [];
  const next = new Map<string, Entry>();

  const place = (key: string, signature: string, render: () => string, index: number) => {
    const cached = entries.get(key);
    if (cached && cached.signature === signature) {
      desired.push({ key, entry: cached, reused: true, index });
      next.set(key, cached);
      return;
    }
    const startedAt = performance.now();
    const entry: Entry = { signature, nodes: build(render(), attach) };
    htmlMs += performance.now() - startedAt;
    // An item can render several sibling roots (tool row, failure preview,
    // sub-agents); any of them can be the reader's anchor.
    if (cached) {
      for (let i = 0; i < Math.min(cached.nodes.length, entry.nodes.length); i++) replaced.set(cached.nodes[i], entry.nodes[i]);
    }
    desired.push({ key, entry, reused: false, index });
    next.set(key, entry);
  };

  if (errorMessage && items.length === 0) {
    place(STATE_KEY, "error:" + errorMessage, () => `<div class="error">${escapeHtml(errorMessage)}</div>`, 0);
  } else if (items.length === 0) {
    place(STATE_KEY, "empty", () => '<div class="empty">No messages yet</div>', 0);
  } else {
    if (errorMessage) place(ERROR_KEY, "banner:" + errorMessage, () => `<div class="error row">${escapeHtml(errorMessage)}</div>`, 0);
    const seen = new Map<string, number>();
    items.forEach((item, index) => {
      const count = seen.get(item.id) ?? 0;
      seen.set(item.id, count + 1);
      const recovered = isRecoveredApiError(items, index);
      const sig = recovered ? `${JSON.stringify(item)}|rec` : JSON.stringify(item);
      place(count ? `${item.id}#${count}` : item.id, sig, () => renderItem(item, index, recovered), index);
    });
  }

  // Capture what the reader did to rows that are about to be rebuilt.
  const carried = new Map<string, UiState>();
  for (const { key, reused } of desired) {
    const old = entries.get(key);
    if (!reused && old) carried.set(key, captureUi(old.nodes));
  }

  const domStartedAt = performance.now();
  const want = new Set<Node>();
  for (const { entry } of desired) for (const node of entry.nodes) want.add(node);
  let cursor: ChildNode | null = root.firstChild;
  const dropUnwanted = () => {
    while (cursor && !want.has(cursor)) {
      const stale = cursor;
      cursor = cursor.nextSibling;
      stale.remove();
    }
  };
  for (const { entry } of desired) {
    for (const node of entry.nodes) {
      dropUnwanted();
      if (node === cursor) {
        cursor = cursor.nextSibling;
      } else {
        root.insertBefore(node, cursor);
      }
    }
  }
  dropUnwanted();
  while (cursor) {
    const stale = cursor;
    cursor = cursor.nextSibling;
    stale.remove();
  }
  for (const { key, entry, reused, index } of desired) {
    if (reused) updateIndices(entry.nodes, index);
    else {
      const state = carried.get(key);
      if (state) applyUi(entry.nodes, state);
      updateIndices(entry.nodes, index);
    }
  }
  domMs = performance.now() - domStartedAt;

  entries = next;
  return { html_ms: htmlMs, dom_ms: domMs, replaced };
}
