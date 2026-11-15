import { postToNative } from "./bridge";
import { escapeHtml } from "./escape";
import type { Subagent, TranscriptItem } from "./types";

function formatCompactDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  if (minutes > 0) return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`;
  return `${seconds}s`;
}

function formatCompactTokens(tokens: number): string {
  const safe = Math.max(0, Math.round(tokens));
  if (safe >= 1_000_000) {
    const m = safe / 1_000_000;
    return `${m >= 10 ? Math.round(m) : m.toFixed(1)}M tok`;
  }
  if (safe >= 1_000) {
    const k = safe / 1_000;
    if (k >= 100) return `${Math.round(k)}k tok`;
    const r = k.toFixed(1);
    return `${r.endsWith(".0") ? r.slice(0, -2) : r}k tok`;
  }
  return `${safe} tok`;
}

function formatChildMeta(child: Subagent): string {
  const parts: string[] = [];
  if (typeof child.durationMs === "number" && Number.isFinite(child.durationMs) && child.durationMs >= 0) {
    parts.push(formatCompactDuration(child.durationMs));
  }
  if (typeof child.totalTokens === "number" && Number.isFinite(child.totalTokens) && child.totalTokens > 0) {
    parts.push(formatCompactTokens(child.totalTokens));
  }
  parts.push(`${child.toolCalls}${child.toolCalls === 1 ? " call" : " calls"}`);
  return parts.join(" · ");
}

/// Workers this tool call spawned, opened in place. Not spliced into this
/// transcript: a 22-agent fan-out inlined would bury the session it belongs
/// to. Tapping one asks the app to open that child.
export function subagentNode(item: TranscriptItem): string {
  const children = item.subagents || [];
  if (!children.length) return "";
  const rows = children.map((child, idx) => {
    const depth = typeof child.depth === "number" && child.depth > 0 ? Math.min(child.depth, 4) : 0;
    const indentAttr = depth > 0 ? ` data-depth="${depth}" style="margin-left:${depth * 14}px"` : "";
    const liveStatus = child.liveStatus?.trim();
    const statusDot = liveStatus
      ? `<span class="subagent-dot status-${escapeHtml(liveStatus)}" aria-label="${escapeHtml(liveStatus)}"></span>`
      : "";
    const typePill = child.agentType
      ? `<span class="subagent-pill">${escapeHtml(child.agentType)}</span>`
      : "";
    const forkBadge = child.isFork ? '<span class="subagent-pill fork">fork</span>' : "";
    const waitTag =
      liveStatus === "waiting" ? '<span class="subagent-status waiting">waiting</span>' : "";
    const branchGlyph =
      children.length === 1 && idx === 0 ? '<span class="subagents-branch" aria-hidden="true">└─</span>' : "";
    const buttonHtml =
      `${branchGlyph}<button type="button" class="subagent-link" data-subagent-session="` +
      escapeHtml(child.sessionId) +
      '">' +
      statusDot +
      typePill +
      forkBadge +
      '<span class="subagent-label">' +
      escapeHtml(child.label) +
      "</span>" +
      waitTag +
      '<span class="subagent-meta">' +
      escapeHtml(formatChildMeta(child)) +
      "</span>" +
      "</button>";
    return children.length === 1 ? buttonHtml : `<li${indentAttr}>${buttonHtml}</li>`;
  });
  if (children.length === 1) {
    return `<div class="subagents subagents-single" data-open-key="${escapeHtml(item.id)}">${rows[0]}</div>`;
  }
  const rawSummary = (item.subagentSummary || "").trim();
  const summaryLabel =
    !rawSummary || (rawSummary === "1 agent" && children.length > 1)
      ? `${children.length} agents`
      : rawSummary;
  return (
    '<details class="subagents" data-open-key="' +
    escapeHtml(item.id) +
    '"><summary>' +
    escapeHtml(summaryLabel) +
    '</summary><ul class="subagent-list">' +
    rows.join("") +
    "</ul></details>"
  );
}

export function attachSubagentHandlers(scope: ParentNode = document): void {
  for (const button of scope.querySelectorAll("[data-subagent-session]")) {
    button.addEventListener("click", () => {
      const sessionId = button.getAttribute("data-subagent-session");
      if (!sessionId) return;
      postToNative({ type: "openSubagent", sessionId });
    });
  }
}
