import { renderDiff } from "./diffs";
import { escapeHtml } from "./escape";
import { mediaStrip } from "./media";
import { subagentNode } from "./subagents";
import type { ToolCall, TranscriptItem } from "./types";

/// A lite page sent this row's bodies as previews. Expanding asks native for
/// the full ones (`liteBodies.ts`); the open key keeps the row open across the
/// re-render that delivers them.
function bodyAttributes(item: TranscriptItem): string {
  const cursors = item.bodyCursors || [];
  const openKey = ' data-open-key="row:' + escapeHtml(item.id) + '"';
  return cursors.length ? openKey + ' data-body-cursors="' + escapeHtml(cursors.join(" ")) + '"' : openKey;
}

function bodyNote(item: TranscriptItem): string {
  if (!(item.bodyCursors || []).length) return "";
  const text =
    item.bodyState === "unavailable"
      ? "The full output is no longer available; this is a preview."
      : "Loading the full output…";
  return '<p class="body-note" role="status">' + escapeHtml(text) + "</p>";
}

export function toolDetails(item: TranscriptItem): string {
  // A failure is never hidden behind a duration: the exit chip wins (R4).
  const isFailure =
    !!item.failurePreview || (typeof item.status === "string" && (item.status.indexOf("exit ") === 0 || item.status === "failed"));
  const meta =
    item.status === "running"
      ? "running"
      : item.status === "dropped"
        ? "dropped"
        : item.status === "orphan"
          ? "orphan"
          : isFailure
            ? "failed"
            : "";
  const status = isFailure ? item.status || "failed" : item.duration || item.status || "";
  // A diff replaces the raw input block when the edit shape is known (R3).
  const diffHtml = renderDiff(item.diff);
  const input = diffHtml
    ? diffHtml
    : item.input
      ? '<div class="section-label">Input</div><pre><code>' + escapeHtml(item.input) + "</code></pre>"
      : "";
  const media = mediaStrip(item.media || []);
  const provenance = (item.calls || [])
    .map((call) => {
      const childInput = call.input ? "<pre><code>" + escapeHtml(call.input) + "</code></pre>" : "";
      const rawInput = call.rawInput ? "<pre><code>" + escapeHtml(call.rawInput) + "</code></pre>" : "";
      return (
        '<details class="raw-disclosure"><summary>' +
        escapeHtml(call.title || "Provider evidence") +
        "</summary>" +
        childInput +
        rawInput +
        "</details>"
      );
    })
    .join("");
  let output = "";
  if (item.output) {
    output = '<div class="section-label">Output</div><pre><code>' + escapeHtml(item.output) + "</code></pre>";
  } else if (item.status === "running") {
    output = '<div class="section-label">Output</div><p>Running...</p>';
  } else if (item.status === "dropped") {
    output = "<p>No result recorded, likely dropped during ingest.</p>";
  }
  // Failure preview sits outside <details> so it needs no tap (R4).
  const preview = item.failurePreview
    ? '<div class="failure-preview" role="note"><span class="sr-only">Error output: </span><pre>' +
      escapeHtml(item.failurePreview) +
      "</pre></div>"
    : "";
  return `
        <details class="tool row"${bodyAttributes(item)}>
          <summary>
            <span class="tool-title">${escapeHtml(item.title || "Tool")}</span>
            <span class="tool-subtitle">${escapeHtml(item.subtitle || "")}</span>
            <span class="tool-meta ${meta}">${escapeHtml(status)}</span>
          </summary>
          <div class="details-body">${input}${provenance}${output}${media}${bodyNote(item)}</div>
        </details>
        ${preview}
        ${subagentNode(item)}
      `;
}

export function activityGroup(item: TranscriptItem): string {
  const all = item.calls || [];
  const visibleLimit = 8;
  const earlierCount = Math.max(0, all.length - visibleLimit);
  const renderCall = (call: ToolCall) => {
    const input = call.input ? '<div class="section-label">Input</div><pre><code>' + escapeHtml(call.input) + "</code></pre>" : "";
    const rawInput = call.rawInput
      ? '<details class="raw-disclosure"><summary>Raw enclosing wrapper</summary><pre><code>' +
        escapeHtml(call.rawInput) +
        "</code></pre></details>"
      : "";
    const output = call.output ? '<div class="section-label">Output</div><pre><code>' + escapeHtml(call.output) + "</code></pre>" : "";
    const media = mediaStrip(call.media || []);
    return `
          <div class="passive-call">
            <div class="passive-call-title">${escapeHtml(call.title || "Tool")}</div>
            <div class="passive-call-subtitle">${escapeHtml(call.subtitle || "")}</div>
            ${input}${rawInput}${output}${media}
          </div>
        `;
  };
  const earlierHtml = earlierCount > 0 ? all.slice(0, earlierCount).map(renderCall).join("") : "";
  const latestHtml = all.slice(earlierCount).map(renderCall).join("");
  const earlierControl =
    earlierCount > 0
      ? `<button type="button" class="passive-earlier-btn" onclick="this.nextElementSibling.hidden=false;this.remove();">Show ${earlierCount} earlier</button><div class="passive-earlier" hidden>${earlierHtml}</div>`
      : "";
  return `
        <details class="passive row"${bodyAttributes(item)}>
          <summary>
            <span class="tool-title">${escapeHtml(item.title || "Activity")}</span>
            <span class="tool-subtitle">${escapeHtml(item.subtitle || "")}</span>
          </summary>
          <div class="details-body">${earlierControl}${latestHtml}${bodyNote(item)}</div>
        </details>
        ${subagentNode(item)}
      `;
}

export function question(item: TranscriptItem): string {
  const options = (item.calls || [])
    .map(
      (call) => `
        <div class="question-option" aria-disabled="true">
          <span class="question-option-title">${escapeHtml(call.title || "")}</span>
          ${call.subtitle ? `<span class="question-option-subtitle">${escapeHtml(call.subtitle)}</span>` : ""}
        </div>
      `,
    )
    .join("");
  return `
        <article class="row question">
          <div class="question-eyebrow">Needs answer</div>
          <div class="question-title">${escapeHtml(item.title || "Question")}</div>
          <div class="question-subtitle">${escapeHtml(item.subtitle || "Answer in terminal")}</div>
          <div class="question-body">${escapeHtml(item.body || "Claude is waiting for your answer.")}</div>
          ${options ? `<div class="question-options" aria-label="Answer options">${options}</div>` : ""}
        </article>
      `;
}
