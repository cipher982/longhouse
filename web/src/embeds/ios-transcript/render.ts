import { reconcile, resetReconciler } from "./reconcile";
import { markdownToHtml } from "./markdown";
import { attachSubmittedInputHandlers } from "./rows";
import { captureAnchor, isStickingToBottom, resetScrollForTesting, retargetAnchor, setStickToBottom, settle } from "./scroll";
import { presentTranscript } from "../../shared/session/model/presentTranscript";
import { attachSubagentHandlers } from "./subagents";
import { attachToolBodyHandlers } from "./liteBodies";
import { updateRunningTimers } from "./toolCards";
import type { FrameMetrics, RenderMetrics, TranscriptItem, TranscriptPayload, TranscriptProjection } from "./types";

let currentItems: TranscriptItem[] = [];
const transcriptFrames = new Map<number, Promise<FrameMetrics>>();
let runningTimerHandle: ReturnType<typeof setInterval> | null = null;

function syncRunningTimerInterval(root: ParentNode): void {
  const count = updateRunningTimers(root);
  if (count > 0 && runningTimerHandle === null) {
    runningTimerHandle = setInterval(() => {
      const container = document.getElementById("root");
      if (!container || updateRunningTimers(container) === 0) {
        if (runningTimerHandle !== null) {
          clearInterval(runningTimerHandle);
          runningTimerHandle = null;
        }
      }
    }, 1000);
  } else if (count === 0 && runningTimerHandle !== null) {
    clearInterval(runningTimerHandle);
    runningTimerHandle = null;
  }
}

/// Test seam: a fresh document state, as after a document load.
export function resetTranscriptStateForTesting(): void {
  currentItems = [];
  transcriptFrames.clear();
  if (runningTimerHandle !== null) {
    clearInterval(runningTimerHandle);
    runningTimerHandle = null;
  }
  resetReconciler();
  resetScrollForTesting();
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

/// Native sends served events and its own state; the shared model builds the
/// rows, so this document and the web timeline cannot disagree.
function presentProjection(projection: TranscriptProjection): TranscriptItem[] {
  const lite = projection.liteBodies;
  return presentTranscript({
    items: projection.items,
    subagents: projection.subagents,
    serverUrl: projection.serverUrl,
    overlay: projection.overlay,
    liteBodies: lite
      ? {
          bodies: new Map((lite.bodies ?? []).map((body) => [body.cursor, body])),
          loading: new Set(lite.loading ?? []),
          failed: new Set(lite.failed ?? []),
          unavailable: new Set(lite.unavailable ?? []),
        }
      : undefined,
  });
}

function attachExpandHandlers(scope: ParentNode = document): void {
  attachSubagentHandlers(scope);
  attachSubmittedInputHandlers(scope);
  attachToolBodyHandlers(scope);
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

/// `window.renderTranscript(base64, stick, sequence, mode)`: the only way
/// native puts content on the page. `mode` is accepted for the benchmark
/// harness and ignored: there is one renderer.
export function renderTranscript(
  base64: string,
  shouldStickToBottom: unknown,
  sequence: number,
  _renderMode?: string,
): RenderMetrics {
  const startedAt = performance.now();
  // Native owns this decision. Re-reading geometry here is what let a stale
  // opinion override a deliberate scroll away from the bottom.
  setStickToBottom(shouldStickToBottom);
  const decodeStartedAt = performance.now();
  const payload = decodePayload(base64);
  const decodeMs = performance.now() - decodeStartedAt;
  const presentStartedAt = performance.now();
  currentItems = payload.projection ? presentProjection(payload.projection) : payload.items || [];
  const presentMs = payload.projection ? performance.now() - presentStartedAt : 0;
  const root = document.getElementById("root")!;
  // The reader's place, taken before anything changes.
  if (!isStickingToBottom()) captureAnchor();
  const result = reconcile(root, currentItems, payload.errorMessage, attachExpandHandlers);
  syncRunningTimerInterval(root);
  retargetAnchor(result.replaced);
  settle("render");
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
    present_ms: presentMs,
    row_count: currentItems.length,
    latest_item_id: currentItems.length > 0 ? currentItems[currentItems.length - 1].id : null,
    html_ms: result.html_ms,
    dom_ms: result.dom_ms,
  };
}
