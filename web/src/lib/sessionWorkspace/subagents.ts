/**
 * Binding worker transcripts to the tool call that spawned them.
 *
 * Two shapes, two kinds of evidence, both provider-supplied:
 *
 * - A Task/Agent subagent names its parent tool call in a sidecar the engine
 *   reads at parse time, so the child arrives already carrying
 *   `parent_tool_call_id`. Exact match, nothing to infer.
 * - A Workflow subagent knows only its run id. The one place a run is tied to a
 *   tool call is the parent's own `Workflow` tool result, which names the
 *   transcript directory. That text is already in the transcript, so the
 *   binding is a read of evidence we have — not a heuristic.
 *
 * Fail closed on both. A run id that appears in no tool result leaves its
 * children unbound and the row renders as an ordinary tool call, which is what
 * it is until the provider says otherwise. Never "nearest Workflow call".
 */

import type { SubagentChild } from "./types";

/** `Transcript dir: …/subagents/workflows/<run>` — the literal line, nothing looser. */
const TRANSCRIPT_DIR_RUN = /Transcript dir:\s*\S*?[/\\]subagents[/\\]workflows[/\\]([A-Za-z0-9_-]+)/g;

/** Run ids named by one tool result, in the order they appear. */
export function runIdsInToolOutput(output: string | null | undefined): string[] {
  const text = output || "";
  if (!text.includes("subagents")) return [];
  const found: string[] = [];
  // A fresh regex per call: the shared literal carries lastIndex across uses.
  const pattern = new RegExp(TRANSCRIPT_DIR_RUN.source, "g");
  let match = pattern.exec(text);
  while (match !== null) {
    if (match[1] && !found.includes(match[1])) found.push(match[1]);
    match = pattern.exec(text);
  }
  return found;
}

/**
 * Children spawned by one tool call: those naming it directly, plus every
 * member of a run that call launched.
 */
export function childrenForToolCall(
  children: readonly SubagentChild[],
  { toolCallId, toolOutputText }: { toolCallId: string | null | undefined; toolOutputText: string | null | undefined },
): SubagentChild[] {
  if (children.length === 0) return [];
  const runIds = runIdsInToolOutput(toolOutputText);
  const matched = children.filter((child) => {
    if (toolCallId && child.parent_tool_call_id === toolCallId) return true;
    return Boolean(child.run_id) && runIds.includes(child.run_id as string);
  });
  return matched.sort((left, right) => (left.started_at || "").localeCompare(right.started_at || ""));
}

/** "22 agents · 4m12s" — the shape of the work, before anyone expands it. */
export function summarizeSubagents(children: readonly SubagentChild[]): string {
  if (children.length === 0) return "";
  const label = children.length === 1 ? "1 agent" : `${children.length} agents`;
  const starts = children.map((child) => child.started_at).filter(Boolean) as string[];
  const ends = children.map((child) => child.ended_at).filter(Boolean) as string[];
  if (starts.length === 0 || ends.length !== children.length) return label;
  const spanMs = Math.max(...ends.map((value) => Date.parse(value))) - Math.min(...starts.map((value) => Date.parse(value)));
  if (!Number.isFinite(spanMs) || spanMs <= 0) return label;
  const seconds = Math.round(spanMs / 1000);
  const duration = seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, "0")}s`;
  return `${label} · ${duration}`;
}

/** One child's line: its own title, or the prompt it was handed. */
export function subagentLabel(child: SubagentChild): string {
  const candidate = (child.title || child.first_user_message_preview || "").trim().replace(/\s+/g, " ");
  if (!candidate) return child.session_id.slice(0, 8);
  return candidate.length > 80 ? `${candidate.slice(0, 79)}…` : candidate;
}
