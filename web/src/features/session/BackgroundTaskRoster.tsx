import { useId, useMemo, useState } from "react";
import { Link, useInRouterContext } from "react-router";
import type { SessionStateFacts } from "@/shared/api/agents";
import { formatCompactTokens, formatTurnDuration } from "@/shared/session/model";
import { parseUTC } from "@/shared/lib/dateUtils";

type DelegationTask = NonNullable<NonNullable<SessionStateFacts["delegation"]>["items"]>[number];

type TaskCategory = "agent" | "shell" | "monitor";

interface RosterTreeNode {
  task: DelegationTask;
  depth: number;
}

function classifyTaskCategory(kind: string | null | undefined): TaskCategory {
  const normalized = (kind ?? "").trim().toLowerCase();
  if (normalized.includes("monitor")) return "monitor";
  if (normalized === "shell" || normalized === "bash" || normalized === "command") return "shell";
  return "agent";
}

function normalizeTaskAgentKey(id: string | null | undefined): string | null {
  if (!id) return null;
  const trimmed = id.trim();
  if (!trimmed) return null;
  return trimmed.startsWith("agent-") ? trimmed.slice(6) : trimmed;
}

function buildRosterTree(items: readonly DelegationTask[]): RosterTreeNode[] {
  if (items.length === 0) return [];
  const byAgentKey = new Map<string, DelegationTask>();
  for (const item of items) {
    const key = normalizeTaskAgentKey(item.id);
    if (key && !byAgentKey.has(key)) byAgentKey.set(key, item);
  }

  const childrenMap = new Map<string, DelegationTask[]>();
  const roots: DelegationTask[] = [];
  for (const item of items) {
    const parentKey = normalizeTaskAgentKey(item.parent_agent_id);
    const parent = parentKey ? byAgentKey.get(parentKey) : undefined;
    if (parent && parent.id !== item.id) {
      const list = childrenMap.get(parent.id) ?? [];
      list.push(item);
      childrenMap.set(parent.id, list);
    } else {
      roots.push(item);
    }
  }

  const visited = new Set<string>();
  const flattened: RosterTreeNode[] = [];
  const walk = (task: DelegationTask, depth: number) => {
    if (visited.has(task.id)) return;
    visited.add(task.id);
    flattened.push({ task, depth });
    for (const child of childrenMap.get(task.id) ?? []) {
      walk(child, depth + 1);
    }
  };
  for (const root of roots) {
    walk(root, 0);
  }
  return flattened;
}

function taskStatusTone(task: DelegationTask): "running" | "waiting" {
  const status = (task.native_progress?.status ?? task.status ?? "").trim().toLowerCase();
  return status === "waiting" ? "waiting" : "running";
}

function formatTaskElapsedMs(task: DelegationTask, nowMs: number): number | null {
  if (!task.ended_at) {
    const anchor = task.started_at ?? task.first_observed_at ?? task.registered_at;
    if (anchor) {
      const startMs = parseUTC(anchor).getTime();
      if (Number.isFinite(startMs)) {
        return Math.max(task.native_progress?.duration_ms ?? 0, nowMs - startMs);
      }
    }
  }
  if (typeof task.native_progress?.duration_ms === "number" && Number.isFinite(task.native_progress.duration_ms)) {
    return Math.max(0, task.native_progress.duration_ms);
  }
  return null;
}

export function summarizeBackgroundRoster(
  items: readonly DelegationTask[],
  pendingBackgroundAgentCount: number | null | undefined,
): string {
  const pendingCount = pendingBackgroundAgentCount ?? 0;
  if (items.length === 0) {
    return `Waiting for ${pendingCount} background ${pendingCount === 1 ? "agent" : "agents"}`;
  }

  const agents = items.filter((item) => classifyTaskCategory(item.kind) === "agent");
  const shells = items.filter((item) => classifyTaskCategory(item.kind) === "shell");
  const monitors = items.filter((item) => classifyTaskCategory(item.kind) === "monitor");

  const parts: string[] = [];
  if (agents.length > 0) {
    const waitingCount = agents.filter((item) => taskStatusTone(item) === "waiting").length;
    const runningCount = agents.length - waitingCount;
    const base = `${agents.length} ${agents.length === 1 ? "agent" : "agents"}`;
    if (waitingCount > 0 && runningCount > 0) {
      parts.push(`${base} (${runningCount} running, ${waitingCount} waiting)`);
    } else if (waitingCount > 0) {
      parts.push(`${base} (waiting)`);
    } else {
      parts.push(base);
    }
  } else if (pendingCount > 0) {
    parts.push(`Waiting for ${pendingCount} ${pendingCount === 1 ? "agent" : "agents"}`);
  }

  if (shells.length > 0) {
    parts.push(`${shells.length} ${shells.length === 1 ? "shell" : "shells"}`);
  }
  if (monitors.length > 0) {
    parts.push(`${monitors.length} ${monitors.length === 1 ? "monitor" : "monitors"}`);
  }
  return parts.join(" · ");
}

export function BackgroundTaskRoster({
  delegation,
  pendingBackgroundAgentCount,
  nowMs,
}: {
  delegation: SessionStateFacts["delegation"] | null | undefined;
  pendingBackgroundAgentCount?: number | null;
  nowMs: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  const inRouter = useInRouterContext();
  const activeItems = useMemo(() => delegation?.items ?? [], [delegation?.items]);
  const pendingCount = pendingBackgroundAgentCount ?? 0;
  const rows = useMemo(() => buildRosterTree(activeItems), [activeItems]);

  if (activeItems.length === 0 && pendingCount <= 0) {
    return null;
  }

  const summary = summarizeBackgroundRoster(activeItems, pendingCount);
  const canExpand = rows.length > 0;

  return (
    <div
      className={`session-background-roster${expanded ? " is-expanded" : ""}`}
      data-testid="session-background-roster"
    >
      {canExpand ? (
        <button
          type="button"
          className="session-background-roster__summary"
          data-testid="session-background-roster-toggle"
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={() => setExpanded((value) => !value)}
        >
          <span className={`session-background-roster__chev${expanded ? " is-open" : ""}`} aria-hidden="true">
            ›
          </span>
          <span className="session-background-roster__label">{summary}</span>
        </button>
      ) : (
        <div className="session-background-roster__summary session-background-roster__summary--static">
          <span className="session-background-roster__label">{summary}</span>
        </div>
      )}

      {canExpand && expanded ? (
        <ul
          id={listId}
          className="session-background-roster__list"
          data-testid="session-background-roster-list"
        >
          {rows.map(({ task, depth }) => {
            const category = classifyTaskCategory(task.kind);
            const tone = taskStatusTone(task);
            const progress = task.native_progress;
            const title = task.description?.trim() || task.id;
            const stepText =
              progress?.last_intent?.trim() ||
              (progress?.current_tool ? `Using ${progress.current_tool}` : null) ||
              (tone === "waiting" ? "Waiting on background work" : null);
            const elapsedMs = formatTaskElapsedMs(task, nowMs);
            const tokens = progress?.tokens ?? progress?.context_tokens ?? null;
            const kindLabel =
              category === "monitor"
                ? "monitor"
                : category === "shell"
                  ? "shell"
                  : task.agent_type?.trim() || "agent";
            const metaParts: string[] = [];
            if (elapsedMs != null) metaParts.push(formatTurnDuration(elapsedMs));
            if (tokens != null && tokens > 0) metaParts.push(`${formatCompactTokens(tokens)} tok`);
            if (progress?.tool_count != null && progress.tool_count > 0) {
              metaParts.push(`${progress.tool_count} ${progress.tool_count === 1 ? "tool" : "tools"}`);
            }

            return (
              <li
                key={task.id}
                className="session-background-roster__item"
                data-testid="session-background-roster-item"
                data-task-kind={category}
                data-task-status={tone}
                data-spawn-depth={depth}
                style={depth > 0 ? { paddingLeft: `${depth * 14}px` } : undefined}
              >
                <span className={`session-background-roster__dot session-background-roster__dot--${tone}`} aria-hidden="true" />
                <span className="session-background-roster__badge">{kindLabel}</span>
                {task.is_fork ? <span className="session-background-roster__badge session-background-roster__badge--fork">fork</span> : null}
                <span className="session-background-roster__status">{tone}</span>
                <span className="session-background-roster__title">
                  {task.session_id ? (
                    inRouter ? (
                      <Link className="session-background-roster__link" to={`/timeline/${task.session_id}`}>
                        {title}
                      </Link>
                    ) : (
                      <a className="session-background-roster__link" href={`/timeline/${task.session_id}`}>
                        {title}
                      </a>
                    )
                  ) : (
                    title
                  )}
                </span>
                {stepText ? (
                  <span className="session-background-roster__step" title={stepText}>
                    {stepText}
                  </span>
                ) : null}
                {metaParts.length > 0 ? (
                  <span className="session-background-roster__meta">{metaParts.join(" · ")}</span>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
