import { escapeHtml } from "./escape";
import { markdownToHtml } from "./markdown";
import { renderItem } from "./rows";
import { isStickingToBottom, scrollToBottom, setStickToBottom } from "./scroll";
import { attachSubagentHandlers, captureOpenSubagentKeys, restoreOpenSubagentKeys } from "./subagents";
import type { FrameMetrics, RenderMetrics, TranscriptItem, TranscriptPayload } from "./types";

let currentItems: TranscriptItem[] = [];
const transcriptFrames = new Map<number, Promise<FrameMetrics>>();
/// Retained mode (the "retained-webkit" benchmark renderer): one DOM node per
/// item id, reused while the item's JSON signature is unchanged. Only the
/// item's first root element is kept.
const retainedItems = new Map<string, { signature: string; node: Element }>();

/// Test seam: a fresh document state, as after a document load.
export function resetTranscriptStateForTesting(): void {
  currentItems = [];
  transcriptFrames.clear();
  retainedItems.clear();
  setStickToBottom(true);
}

/// `window.waitForTranscriptFrame`: resolves once the frame a render queued
/// has painted, with its timings.
export async function waitForTranscriptFrame(sequence: number): Promise<FrameMetrics> {
  const frame = transcriptFrames.get(sequence);
  if (!frame) throw new Error(`missing transcript frame ${sequence}`);
  const metrics = await frame;
  transcriptFrames.delete(sequence);
  return metrics;
}

export function decodePayload(base64: string): TranscriptPayload {
  const binary = atob(base64);
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

function attachExpandHandlers(scope: ParentNode = document): void {
  attachSubagentHandlers(scope);
  for (const button of scope.querySelectorAll("[data-expand-index]")) {
    button.addEventListener("click", () => {
      const index = Number(button.getAttribute("data-expand-index"));
      const item = currentItems[index];
      if (!item || !item.fullBody) return;
      const article = button.closest("[data-message-index]");
      if (article) {
        const content = article.querySelector(".message-content")!;
        content.innerHTML = markdownToHtml(item.fullBody);
      } else {
        const bubble = button.parentElement!.querySelector(".bubble")!;
        bubble.textContent = item.fullBody;
      }
      button.remove();
    });
  }
}

function updateRetainedIndices(node: Element, index: number): void {
  if (node.hasAttribute("data-message-index")) {
    node.setAttribute("data-message-index", String(index));
  }
  for (const button of node.querySelectorAll("[data-expand-index]")) {
    button.setAttribute("data-expand-index", String(index));
  }
}

function retainedHTML(payload: TranscriptPayload, root: Element): { html_ms: number; dom_ms: number } {
  let htmlMs = 0;
  if (payload.errorMessage && currentItems.length === 0) {
    retainedItems.clear();
    const startedAt = performance.now();
    const html = `<div class="error">${escapeHtml(payload.errorMessage)}</div>`;
    htmlMs += performance.now() - startedAt;
    const domStartedAt = performance.now();
    root.innerHTML = html;
    return { html_ms: htmlMs, dom_ms: performance.now() - domStartedAt };
  }
  if (currentItems.length === 0) {
    retainedItems.clear();
    const domStartedAt = performance.now();
    root.innerHTML = '<div class="empty">No messages yet</div>';
    return { html_ms: htmlMs, dom_ms: performance.now() - domStartedAt };
  }

  const nextItems = new Map<string, { signature: string; node: Element }>();
  const fragment = document.createDocumentFragment();
  if (payload.errorMessage) {
    const startedAt = performance.now();
    const error = document.createElement("div");
    error.className = "error row";
    error.textContent = payload.errorMessage;
    htmlMs += performance.now() - startedAt;
    fragment.appendChild(error);
  }

  currentItems.forEach((item, index) => {
    const signature = JSON.stringify(item);
    const cached = retainedItems.get(item.id);
    let node: Element | null;
    if (cached && cached.signature === signature) {
      node = cached.node;
      updateRetainedIndices(node, index);
    } else {
      const startedAt = performance.now();
      const template = document.createElement("template");
      template.innerHTML = renderItem(item, index).trim();
      node = template.content.firstElementChild;
      htmlMs += performance.now() - startedAt;
      if (!node) return;
      attachExpandHandlers(node);
    }
    nextItems.set(item.id, { signature, node });
    fragment.appendChild(node);
  });

  const domStartedAt = performance.now();
  root.replaceChildren(fragment);
  const domMs = performance.now() - domStartedAt;
  retainedItems.clear();
  for (const [id, entry] of nextItems) retainedItems.set(id, entry);
  return { html_ms: htmlMs, dom_ms: domMs };
}

/// `window.renderTranscript(base64, stick, sequence, mode)`: the only way
/// native puts content on the page. `mode` is "retained" for the retained
/// benchmark renderer; anything else rebuilds the root from HTML.
export function renderTranscript(
  base64: string,
  shouldStickToBottom: unknown,
  sequence: number,
  renderMode?: string,
): RenderMetrics {
  const startedAt = performance.now();
  // Native owns this decision. Re-reading geometry here is what let a stale
  // opinion override a deliberate scroll away from the bottom.
  setStickToBottom(shouldStickToBottom);
  const wasAtBottom = isStickingToBottom();
  const previousItems = currentItems;
  const previousFirstId = previousItems.length > 0 ? previousItems[0].id : null;
  const previousScrollHeight = document.documentElement.scrollHeight;
  const previousScrollY = window.scrollY;
  const decodeStartedAt = performance.now();
  const payload = decodePayload(base64);
  const decodeMs = performance.now() - decodeStartedAt;
  currentItems = payload.items || [];
  const newFirstId = currentItems.length > 0 ? currentItems[0].id : null;
  const prepended =
    previousFirstId && newFirstId && previousFirstId !== newFirstId && currentItems.some((item) => item.id === previousFirstId);
  const root = document.getElementById("root")!;
  const openSubagentKeys = captureOpenSubagentKeys(root);
  let htmlMs: number;
  let domMs: number;
  if (renderMode === "retained") {
    const retainedMetrics = retainedHTML(payload, root);
    htmlMs = retainedMetrics.html_ms;
    domMs = retainedMetrics.dom_ms;
  } else {
    retainedItems.clear();
    const htmlStartedAt = performance.now();
    let html: string;
    if (payload.errorMessage && currentItems.length === 0) {
      html = `<div class="error">${escapeHtml(payload.errorMessage)}</div>`;
    } else if (currentItems.length === 0) {
      html = '<div class="empty">No messages yet</div>';
    } else {
      const error = payload.errorMessage ? `<div class="error row">${escapeHtml(payload.errorMessage)}</div>` : "";
      html = error + currentItems.map(renderItem).join("");
    }
    htmlMs = performance.now() - htmlStartedAt;
    const domStartedAt = performance.now();
    root.innerHTML = html;
    attachExpandHandlers(root);
    domMs = performance.now() - domStartedAt;
  }
  restoreOpenSubagentKeys(root, openSubagentKeys);
  if (wasAtBottom) scrollToBottom();
  else if (prepended) {
    const delta = document.documentElement.scrollHeight - previousScrollHeight;
    window.scrollTo(0, previousScrollY + delta);
  }
  const rafStartedAt = performance.now();
  const frame = new Promise<FrameMetrics>((resolve) => {
    requestAnimationFrame(() => {
      resolve({
        raf_ms: performance.now() - rafStartedAt,
        total_ms: performance.now() - startedAt,
      });
    });
  });
  transcriptFrames.set(sequence, frame);
  return {
    decode_ms: decodeMs,
    html_ms: htmlMs,
    dom_ms: domMs,
  };
}
