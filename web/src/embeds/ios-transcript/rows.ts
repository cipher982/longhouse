import { escapeHtml } from "./escape";
import { markdownToHtml } from "./markdown";
import { mediaStrip } from "./media";
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
  return `
        <div class="row message user submitted ${escapeHtml(item.status || "")}">
          <div>
            <div class="bubble">${escapeHtml(item.body || "")}</div>
            <div class="submitted-status">${escapeHtml(item.subtitle || "")}</div>
          </div>
        </div>
      `;
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

export function providerNotification(item: TranscriptItem): string {
  return `
        <div class="row provider-notification" data-testid="session-provider-notification">
          <span class="provider-notification-mark" aria-hidden="true">•</span>
          <span class="provider-notification-body">${escapeHtml(item.body || "Provider update")}</span>
        </div>
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
