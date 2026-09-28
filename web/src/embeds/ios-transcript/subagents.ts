import { postToNative } from "./bridge";
import { escapeHtml } from "./escape";
import type { TranscriptItem } from "./types";

/// Workers this tool call spawned, opened in place. Not spliced into this
/// transcript: a 22-agent fan-out inlined would bury the session it belongs
/// to. Tapping one asks the app to open that child.
export function subagentNode(item: TranscriptItem): string {
  const children = item.subagents || [];
  if (!children.length) return "";
  const rows = children
    .map(
      (child) =>
        '<li><button type="button" class="subagent-link" data-subagent-session="' +
        escapeHtml(child.sessionId) +
        '">' +
        '<span class="subagent-label">' +
        escapeHtml(child.label) +
        "</span>" +
        '<span class="subagent-meta">' +
        String(child.toolCalls) +
        (child.toolCalls === 1 ? " call" : " calls") +
        "</span>" +
        "</button></li>",
    )
    .join("");
  return (
    '<details class="subagents" data-subagent-key="' +
    escapeHtml(item.id) +
    '"><summary>' +
    escapeHtml(item.subagentSummary || "") +
    '</summary><ul class="subagent-list">' +
    rows +
    "</ul></details>"
  );
}

/// A re-render rebuilds the disclosure, so remember which ones the user opened.
export function captureOpenSubagentKeys(root: ParentNode): Set<string> {
  return new Set(
    Array.from(root.querySelectorAll("details.subagents[data-subagent-key][open]"))
      .map((node) => node.getAttribute("data-subagent-key"))
      .filter((key): key is string => Boolean(key)),
  );
}

export function restoreOpenSubagentKeys(root: ParentNode, keys: Set<string>): void {
  if (!keys.size) return;
  root.querySelectorAll<HTMLDetailsElement>("details.subagents[data-subagent-key]").forEach((node) => {
    if (keys.has(node.getAttribute("data-subagent-key") ?? "")) node.open = true;
  });
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
