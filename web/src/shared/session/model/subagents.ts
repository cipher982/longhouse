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
 * member of a run that call launched, and any nested grandchildren.
 */
function normalizeAgentKey(id: string | null | undefined): string | null {
  if (!id) return null;
  const trimmed = id.trim();
  if (!trimmed) return null;
  return trimmed.startsWith("agent-") ? trimmed.slice(6) : trimmed;
}

export function childrenForToolCall(
  children: readonly SubagentChild[],
  { toolCallId, toolOutputText }: { toolCallId: string | null | undefined; toolOutputText: string | null | undefined },
): SubagentChild[] {
  if (children.length === 0) return [];
  const runIds = runIdsInToolOutput(toolOutputText);
  const direct = children.filter((child) => {
    if (toolCallId && child.parent_tool_call_id === toolCallId) return true;
    return Boolean(child.run_id) && runIds.includes(child.run_id as string);
  });
  if (direct.length === 0) return [];

  const seen = new Set<string>(direct.map((child) => child.session_id));
  const collected = [...direct];
  const queue = [...direct];
  while (queue.length > 0) {
    const parent = queue.shift()!;
    const parentAgentKey = normalizeAgentKey(parent.provider_session_id);
    for (const candidate of children) {
      if (seen.has(candidate.session_id)) continue;
      const matchesParentSession =
        Boolean(candidate.parent_session_id) && candidate.parent_session_id === parent.session_id;
      const candidateAgentKey = normalizeAgentKey(candidate.parent_agent_id);
      const matchesParentAgent =
        Boolean(parentAgentKey) && Boolean(candidateAgentKey) && candidateAgentKey === parentAgentKey;
      if (matchesParentSession || matchesParentAgent) {
        seen.add(candidate.session_id);
        collected.push(candidate);
        queue.push(candidate);
      }
    }
  }
  return collected.sort((left, right) => (left.started_at || "").localeCompare(right.started_at || ""));
}

export type SubagentTreeNode = {
  child: SubagentChild;
  depth: number;
  children: SubagentTreeNode[];
};

export function buildSubagentTree(children: readonly SubagentChild[]): SubagentTreeNode[] {
  if (children.length === 0) return [];
  const bySessionId = new Map<string, SubagentChild>();
  const byAgentKey = new Map<string, SubagentChild>();
  for (const child of children) {
    bySessionId.set(child.session_id, child);
    const agentKey = normalizeAgentKey(child.provider_session_id);
    if (agentKey && !byAgentKey.has(agentKey)) {
      byAgentKey.set(agentKey, child);
    }
  }

  const childrenMap = new Map<string, SubagentChild[]>();
  const roots: SubagentChild[] = [];
  for (const child of children) {
    let parent: SubagentChild | undefined;
    if (child.parent_session_id && child.parent_session_id !== child.session_id) {
      parent = bySessionId.get(child.parent_session_id);
    }
    if (!parent) {
      const parentKey = normalizeAgentKey(child.parent_agent_id);
      if (parentKey) {
        const matched = byAgentKey.get(parentKey);
        if (matched && matched.session_id !== child.session_id) {
          parent = matched;
        }
      }
    }
    if (parent) {
      const list = childrenMap.get(parent.session_id) ?? [];
      list.push(child);
      childrenMap.set(parent.session_id, list);
    } else {
      roots.push(child);
    }
  }

  const visited = new Set<string>();
  const buildNode = (child: SubagentChild, depth: number): SubagentTreeNode => {
    visited.add(child.session_id);
    const nested = (childrenMap.get(child.session_id) ?? [])
      .filter((entry) => !visited.has(entry.session_id))
      .map((entry) => buildNode(entry, depth + 1));
    return { child, depth, children: nested };
  };

  return roots.map((root) => buildNode(root, 0));
}

export function flattenSubagentTree(
  children: readonly SubagentChild[],
): Array<{ child: SubagentChild; depth: number }> {
  const flat: Array<{ child: SubagentChild; depth: number }> = [];
  const walk = (nodes: SubagentTreeNode[]) => {
    for (const node of nodes) {
      flat.push({ child: node.child, depth: node.depth });
      walk(node.children);
    }
  };
  walk(buildSubagentTree(children));
  return flat;
}

/** "22 agents · 4m12s" — the shape of the work, before anyone expands it. */
export function summarizeSubagents(children: readonly SubagentChild[]): string {
  if (children.length === 0) return "";
  const label = children.length === 1 ? "1 agent" : `${children.length} agents`;
  const runningCount = children.filter((child) => child.live_status === "running").length;
  const waitingCount = children.filter((child) => child.live_status === "waiting").length;
  const liveParts: string[] = [];
  if (runningCount > 0) liveParts.push(`${runningCount} running`);
  if (waitingCount > 0) liveParts.push(`${waitingCount} waiting`);

  const starts = children.map((child) => child.started_at).filter(Boolean) as string[];
  const ends = children.map((child) => child.ended_at).filter(Boolean) as string[];
  let durationPart: string | null = null;
  if (starts.length > 0 && ends.length === children.length) {
    const spanMs = Math.max(...ends.map((value) => Date.parse(value))) - Math.min(...starts.map((value) => Date.parse(value)));
    if (Number.isFinite(spanMs) && spanMs > 0) {
      const seconds = Math.round(spanMs / 1000);
      durationPart = seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, "0")}s`;
    }
  }
  const parts = [label];
  if (liveParts.length > 0) parts.push(liveParts.join(", "));
  if (durationPart) parts.push(durationPart);
  return parts.join(" · ");
}

/** One child's line: its description, own title, or the prompt it was handed. */
export function subagentLabel(child: SubagentChild): string {
  const candidate = (child.description || child.title || child.first_user_message_preview || child.agent_type || "")
    .trim()
    .replace(/\s+/g, " ");
  if (!candidate) return child.session_id.slice(0, 8);
  return candidate.length > 80 ? `${candidate.slice(0, 79)}…` : candidate;
}
