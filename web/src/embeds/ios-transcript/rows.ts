import { postToNative } from "./bridge";
import { escapeHtml } from "./escape";
import { markdownToHtml } from "./markdown";
import { mediaStrip } from "./media";
import { summarizeProviderNotice } from "../../shared/session/model/providerNotice";
import { activityGroup, question, toolDetails } from "./toolCards";
import type { TranscriptItem } from "./types";

export function message(item: TranscriptItem, index: number): string {
  const body = item.role === "assistant" ? markdownToHtml(item.body || "") : escapeHtml(item.body || "");
  const expand = item.collapsed ? `<button class="expand" data-expand-index="${index}">Show full message</button>` : "";
  const media = mediaStrip(item.media || []);
  if (item.role === "user") {
    const origin =
      item.origin === "longhouse"
        ? '<div id="session-chat-input-origin-longhouse" class="origin" aria-label="Sent via Longhouse">Longhouse</div>'
        : "";
    return `
          <div class="row message user">
            <div>
              <div class="bubble">${body}</div>
              ${media}
              ${expand}
              ${origin}
            </div>
          </div>
        `;
  }
  return `
        <article class="row message assistant" data-message-index="${index}">
          <div class="message-content">${body}</div>
          ${media}
          ${expand}
        </article>
      `;
}

export function submitted(item: TranscriptItem): string {
  const attachments = item.attachments?.length
    ? `<div class="submitted-attachments" data-testid="session-submitted-attachments">${escapeHtml(
        "Attachments · " +
          item.attachments
            .map((attachment) => `${attachment.filename || "image"} (${attachment.mimeType || "file"}, ${attachment.byteSize || 0} bytes)`)
            .join(" · "),
      )}</div>`
    : "";
  const id = escapeHtml(item.id);
  const actions =
    item.status === "couldNotConfirm"
      ? `<div class="submitted-actions">
             <button type="button" data-submitted-action="retry" data-client-request-id="${id}">Retry send</button>
           </div>`
      : item.status === "failed" || item.status === "needsUserDecision"
        ? `<div class="submitted-actions">
               <button type="button" data-submitted-action="edit" data-client-request-id="${id}">Edit</button>
               <button type="button" data-submitted-action="discard" data-client-request-id="${id}">Discard</button>
             </div>`
        : "";
  return `
        <div class="row message user submitted ${escapeHtml(item.status || "")}">
          <div>
            <div class="bubble">${escapeHtml(item.body || "")}</div>
            ${attachments}
            <div class="submitted-status">${escapeHtml(item.subtitle || "")}</div>
            ${actions}
          </div>
        </div>
      `;
}

const SUBMITTED_ACTIONS = {
  edit: "editSubmitted",
  discard: "discardSubmitted",
  retry: "retrySubmitted",
} as const;

export function attachSubmittedInputHandlers(scope: ParentNode = document): void {
  for (const button of scope.querySelectorAll("[data-submitted-action]")) {
    button.addEventListener("click", () => {
      const clientRequestId = button.getAttribute("data-client-request-id");
      const type = SUBMITTED_ACTIONS[button.getAttribute("data-submitted-action") as keyof typeof SUBMITTED_ACTIONS];
      if (!clientRequestId || !type) return;
      postToNative({ type, clientRequestId });
    });
  }
}

export function action(item: TranscriptItem): string {
  const subtitle = item.subtitle ? `<span>${escapeHtml(item.subtitle)}</span>` : "";
  return `
        <div class="row action">
          <span class="action-rule"></span>
          <span class="action-title">${escapeHtml(item.title || "Session action")}</span>
          ${subtitle}
        </div>
      `;
}

/// A background job's notice, as a tool-shaped row: the header is the title,
/// the first line of output the hint, the full text behind a tap. A notice
/// that is only its header has nothing to open, so it is a plain row.
export function providerNotification(item: TranscriptItem): string {
  const { title, hint, body } = summarizeProviderNotice(item.body);
  if (body === null) {
    return `
        <div class="tool row notice static" data-testid="session-provider-notification">
          <div class="tool-head"><span class="tool-title">${escapeHtml(title)}</span></div>
        </div>
      `;
  }
  return `
        <details class="tool row notice" data-testid="session-provider-notification" data-open-key="${escapeHtml(item.id)}">
          <summary>
            <span class="tool-title">${escapeHtml(title)}</span>
            <span class="tool-subtitle">${escapeHtml(hint || "")}</span>
          </summary>
          <div class="details-body"><pre><code>${escapeHtml(body)}</code></pre></div>
        </details>
      `;
}

export function renderItemBody(item: TranscriptItem, index: number): string {
  if (item.kind === "message") return message(item, index);
  if (item.kind === "submitted") return submitted(item);
  if (item.kind === "action") return action(item);
  if (item.kind === "providerNotification") return providerNotification(item);
  if (item.kind === "question") return question(item);
  if (item.kind === "tool") return toolDetails(item);
  if (item.kind === "activityGroup") return activityGroup(item);
  return "";
}

export function renderItem(item: TranscriptItem, index: number): string {
  const html = renderItemBody(item, index);
  if (!html || !item.turnEnd) return html;
  // Each item renders as one root node (retained-node reconciliation
  // relies on it), so the footer lives inside that root.
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  const root = template.content.firstElementChild;
  if (!root) return html;
  root.insertAdjacentHTML(
    "beforeend",
    `<div class="turn-end" data-testid="session-turn-end">✻ ${escapeHtml(item.turnEnd.label)} · ${escapeHtml(item.turnEnd.doneAt)}</div>`,
  );
  return template.innerHTML;
}
