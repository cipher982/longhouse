import { postToNative } from "./bridge";
import { escapeHtml } from "./escape";
import { markdownToHtml } from "./markdown";
import { mediaStrip } from "./media";
import { thoughtProse } from "../../shared/session/model/thoughtProse";
import {
  summarizeApiError,
  summarizeCompactionNotice,
  summarizeProviderNotice,
  summarizeTaskNotification,
} from "../../shared/session/model/providerNotice";
import { activityGroup, question, toolDetails } from "./toolCards";
import type { TranscriptItem } from "./types";

function renderApiErrorBlock(apiError: Record<string, unknown>, body?: string | null, recovered = false): string {
  const summary = summarizeApiError(apiError, body);
  const badgeHtml = summary.badge ? `<span class="api-error-badge">${escapeHtml(summary.badge)}</span>` : "";
  const recoveredClass = recovered ? " recovered" : "";
  if (summary.detail) {
    return `
        <details class="tool row notice api-error-notice${recoveredClass}" data-testid="session-api-error-notice">
          <summary>
            ${badgeHtml}
            <span class="tool-title">${escapeHtml(summary.title)}</span>
          </summary>
          <div class="details-body"><pre><code>${escapeHtml(summary.detail)}</code></pre></div>
        </details>
      `;
  }
  return `
        <div class="tool row notice static api-error-notice${recoveredClass}" data-testid="session-api-error-notice">
          <div class="tool-head">${badgeHtml}<span class="tool-title">${escapeHtml(summary.title)}</span></div>
        </div>
      `;
}

export function message(item: TranscriptItem, index: number, recovered = false): string {
  const body = item.role === "assistant" ? markdownToHtml(item.body || "") : escapeHtml(item.body || "");
  const expand = item.collapsed ? `<button class="expand" data-expand-index="${index}">Show full message</button>` : "";
  const media = mediaStrip(item.media || []);
  if (item.role === "user") {
    const n = item.attachedImages || 0;
    const thumbs = (item.attachmentThumbs || [])
      .map(
        (url) =>
          `<a class="attached-thumb" href="${escapeHtml(url)}" target="_blank" rel="noreferrer noopener"><img src="${escapeHtml(url)}" alt="Attached image" loading="lazy" onerror="this.parentNode.outerHTML='&lt;span class=&quot;attached-chip&quot;&gt;Image expired&lt;/span&gt;'"></a>`,
      )
      .join("");
    const attached = thumbs
      ? `<div class="attached-thumbs" data-testid="session-attached-thumbs">${thumbs}</div>`
      : n > 0
        ? `<div class="attached-chip" data-testid="session-attached-images">${n === 1 ? "1 image attached" : `${n} images attached`}</div>`
        : "";
    const origin =
      item.origin === "longhouse"
        ? '<div id="session-chat-input-origin-longhouse" class="origin" aria-label="Sent via Longhouse">Longhouse</div>'
        : "";
    return `
          <div class="row message user">
            <div>
              ${item.body ? `<div class="bubble">${body}</div>` : ""}
              ${attached}
              ${media}
              ${expand}
              ${origin}
            </div>
          </div>
        `;
  }
  const commentaryClass = item.phase === "commentary" ? " commentary" : "";
  const errorNotice = item.apiError ? renderApiErrorBlock(item.apiError, item.body, recovered) : "";
  return `
        <article class="row message assistant${commentaryClass}" data-message-index="${index}">
          <div class="message-content">${body}</div>
          ${media}
          ${expand}
          ${errorNotice}
        </article>
      `;
}

const DATA_IMAGE_URL = /^data:image\/(?:jpeg|png|webp|gif);base64,[A-Za-z0-9+/=]+$/;

export function submitted(item: TranscriptItem): string {
  // The picture the phone is sending, drawn the way the transcript echo will
  // draw it, so the row does not change shape when the echo lands.
  const list = item.attachments || [];
  const thumbs = list
    .map((attachment) => attachment.thumbDataUrl)
    .filter((url): url is string => typeof url === "string" && DATA_IMAGE_URL.test(url))
    .map((url) => `<span class="attached-thumb"><img src="${escapeHtml(url)}" alt="Attached image"></span>`)
    .join("");
  const unshown = list.length - (thumbs.match(/<img /g) || []).length;
  const chip =
    unshown > 0
      ? `<div class="attached-chip" data-testid="session-submitted-attachments">${unshown === 1 ? "1 image attached" : `${unshown} images attached`}</div>`
      : "";
  const attachments = thumbs
    ? `<div class="attached-thumbs" data-testid="session-submitted-thumbs">${thumbs}</div>${chip}`
    : chip;
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

export function compaction(item: TranscriptItem): string {
  const { title, detail } = summarizeCompactionNotice(item.body);
  const subtitle = detail ? `<span class="compaction-detail">${escapeHtml(detail)}</span>` : "";
  return `
        <div class="row action compaction" data-testid="session-compaction-boundary">
          <span class="action-rule"></span>
          <span class="action-title">${escapeHtml(title)}</span>
          ${subtitle}
        </div>
      `;
}

/// A background job's notice, as a tool-shaped row: the header is the title,
/// the first line of output the hint, the full text behind a tap. A notice
/// that is only its header has nothing to open, so it is a plain row.
/// Longhouse's own notice (origin "longhouse", e.g. a Stop's close receipt) is
/// short server-authored prose whose last sentence may be the point, so it is
/// shown whole, as the web timeline shows it.
export function providerNotification(item: TranscriptItem, recovered = false): string {
  if (item.taskNotification) {
    const summary = summarizeTaskNotification(item.taskNotification, item.body);
    const statusDot = summary.status
      ? `<span class="task-notice-dot status-${escapeHtml(summary.status)}" aria-label="${escapeHtml(summary.status)}"></span>`
      : "";
    const meta = summary.meta ? `<span class="tool-subtitle">${escapeHtml(summary.meta)}</span>` : "";
    const note = summary.note ? `<span class="task-notice-note">${escapeHtml(summary.note)}</span>` : "";
    if (!summary.result) {
      return `
        <div class="tool row notice static task-notice" data-testid="session-task-notification">
          <div class="tool-head">${statusDot}<span class="tool-title">${escapeHtml(summary.title)}</span>${meta}${note}</div>
        </div>
      `;
    }
    return `
        <details class="tool row notice task-notice" data-testid="session-task-notification" data-open-key="${escapeHtml(item.id)}">
          <summary>${statusDot}<span class="tool-title">${escapeHtml(summary.title)}</span>${meta}${note}</summary>
          <div class="details-body"><pre><code>${escapeHtml(summary.result)}</code></pre></div>
        </details>
      `;
  }
  if (typeof item.waitingBackgroundAgents === "number") {
    const count = Math.max(0, Math.round(item.waitingBackgroundAgents));
    const unit = count === 1 ? "agent" : "agents";
    return `
        <div class="tool row notice static waiting-agents" data-testid="session-waiting-background-agents">
          <div class="tool-head"><span class="waiting-agents-icon">*</span><span class="tool-title">Waiting for ${count} background ${unit} to finish</span></div>
        </div>
      `;
  }
  if (item.apiError) {
    return renderApiErrorBlock(item.apiError, item.body, recovered);
  }
  const wakeAttribute = item.wake ? ' data-wake="true"' : "";
  if (item.origin === "longhouse") {
    return `
        <div class="tool row notice static" data-testid="session-provider-notification" data-origin="longhouse"${wakeAttribute}>
          <div class="tool-head"><span class="tool-title">${escapeHtml((item.body || "").trim())}</span></div>
        </div>
      `;
  }
  const { title, hint, body } = summarizeProviderNotice(item.body);
  if (body === null) {
    return `
        <div class="tool row notice static" data-testid="session-provider-notification"${wakeAttribute}>
          <div class="tool-head"><span class="tool-title">${escapeHtml(title)}</span></div>
        </div>
      `;
  }
  return `
        <details class="tool row notice" data-testid="session-provider-notification"${wakeAttribute} data-open-key="${escapeHtml(item.id)}">
          <summary>
            <span class="tool-title">${escapeHtml(title)}</span>
            <span class="tool-subtitle">${escapeHtml(hint || "")}</span>
          </summary>
          <div class="details-body"><pre><code>${escapeHtml(body)}</code></pre></div>
        </details>
      `;
}

/// A provider's reasoning summary. For a model that never writes prose between
/// tool calls (Gemini through OMP) this is the only narration, so it leads its
/// step as readable text clamped to three lines; a tap opens the whole thought.
export function reasoning(item: TranscriptItem): string {
  if (item.reasoningWithheld) {
    return `
        <div class="reasoning row reasoning-withheld" data-testid="session-reasoning-withheld">
          <span class="reasoning-withheld-label">Reasoning withheld</span>
        </div>
      `;
  }
  const text = item.body || "";
  const prose = escapeHtml(thoughtProse(text) || "No reasoning details");
  return `
        <details class="reasoning row" data-testid="session-reasoning" data-open-key="${escapeHtml(item.id)}">
          <summary><span class="reasoning-prose">${prose}</span></summary>
          <div class="details-body message-content">${markdownToHtml(text)}</div>
        </details>
      `;
}

export function renderItemBody(item: TranscriptItem, index: number, recovered = false): string {
  if (item.kind === "message") return message(item, index, recovered);
  if (item.kind === "submitted") return submitted(item);
  if (item.kind === "action") return action(item);
  if (item.kind === "compaction") return compaction(item);
  if (item.kind === "providerNotification") return providerNotification(item, recovered);
  if (item.kind === "reasoning") return reasoning(item);
  if (item.kind === "question") return question(item);
  if (item.kind === "tool") return toolDetails(item);
  if (item.kind === "activityGroup") return activityGroup(item);
  return "";
}

export function renderItem(item: TranscriptItem, index: number, recovered = false): string {
  const html = renderItemBody(item, index, recovered);
  if (!html || !item.turnEnd) return html;
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  const root = template.content.firstElementChild;
  if (!root) return html;
  const turnEndHtml = `<div class="turn-end" data-testid="session-turn-end">✻ ${escapeHtml(item.turnEnd.label)} · ${escapeHtml(item.turnEnd.doneAt)}</div>`;
  // Never bury .turn-end inside <details>, otherwise a collapsed tool or
  // reasoning row hides the turn duration footer. Multi-root items also emit
  // .turn-end as a trailing sibling node (supported by reconcile.ts).
  if (root.tagName === "DETAILS" || template.content.children.length > 1) {
    const footerTemplate = document.createElement("template");
    footerTemplate.innerHTML = turnEndHtml;
    template.content.appendChild(footerTemplate.content);
  } else {
    root.insertAdjacentHTML("beforeend", turnEndHtml);
  }
  return template.innerHTML;
}
