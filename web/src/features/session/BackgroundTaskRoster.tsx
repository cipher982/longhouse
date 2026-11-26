import { resolveToolInfo } from "@/shared/session/model/toolTiers.generated";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Link, useInRouterContext } from "react-router";
import type { SessionStateFacts } from "@/shared/api/agents";
import { useMediaQuery } from "@/shared/hooks/useMediaQuery";
import { formatCompactTokens, formatTurnDuration } from "@/shared/session/model";
import { parseUTC } from "@/shared/lib/dateUtils";

type DelegationTask = NonNullable<NonNullable<SessionStateFacts["delegation"]>["items"]>[number];

type TaskCategory = "agent" | "shell" | "monitor";

interface RosterTreeNode {
  task: DelegationTask;
  depth: number;
}

const MAX_LIVE_TASK_ELAPSED_MS = 86_400_000;
const STALE_WAIT_THRESHOLD_MS = 15 * 60_000;

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
  const agents = items.filter((item) => classifyTaskCategory(item.kind) === "agent");
  const shells = items.filter((item) => classifyTaskCategory(item.kind) === "shell");
  const monitors = items.filter((item) => classifyTaskCategory(item.kind) === "monitor");

  const byAgentKey = new Map<string, DelegationTask>();
  for (const item of agents) {
    const key = normalizeTaskAgentKey(item.id);
    if (key && !byAgentKey.has(key)) byAgentKey.set(key, item);
  }

  const childrenMap = new Map<string, DelegationTask[]>();
  const roots: DelegationTask[] = [];
  for (const item of agents) {
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
  for (const item of shells) flattened.push({ task: item, depth: 0 });
  for (const item of monitors) flattened.push({ task: item, depth: 0 });
  return flattened;
}

function taskStatusTone(task: DelegationTask): "running" | "waiting" {
  const status = (task.native_progress?.status ?? task.status ?? "").trim().toLowerCase();
  return status === "waiting" ? "waiting" : "running";
}

function formatTaskElapsedMs(
  task: DelegationTask,
  nowMs: number,
  delegation: SessionStateFacts["delegation"] | null | undefined,
): number | null {
  const validUntilMs = delegation?.valid_until ? parseUTC(delegation.valid_until).getTime() : Number.NaN;
  const observedMs = delegation?.observed_at ? parseUTC(delegation.observed_at).getTime() : Number.NaN;
  const boundedNowMs = Number.isFinite(validUntilMs)
    ? Math.min(nowMs, validUntilMs)
    : Number.isFinite(observedMs) && nowMs - observedMs > 120_000
      ? observedMs
      : nowMs;
  if (!task.ended_at) {
    const anchor = task.started_at ?? task.first_observed_at ?? task.registered_at;
    if (anchor) {
      const startMs = parseUTC(anchor).getTime();
      if (Number.isFinite(startMs)) {
        const diff = boundedNowMs - startMs;
        if (diff >= 0 && diff <= MAX_LIVE_TASK_ELAPSED_MS) {
          return Math.max(task.native_progress?.duration_ms ?? 0, diff);
        }
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

export function BackgroundTaskRosterList({
  id,
  delegation,
  nowMs,
  onNavigate,
}: {
  id?: string;
  delegation: SessionStateFacts["delegation"] | null | undefined;
  nowMs: number;
  onNavigate?: () => void;
}) {
  const inRouter = useInRouterContext();
  const [expandedIds, setExpandedIds] = useState<ReadonlySet<string>>(() => new Set());
  const activeItems = useMemo(() => delegation?.items ?? [], [delegation?.items]);
  const rows = useMemo(() => buildRosterTree(activeItems), [activeItems]);
  const agentRows = rows.filter(({ task }) => classifyTaskCategory(task.kind) === "agent");
  const otherRows = rows.filter(({ task }) => classifyTaskCategory(task.kind) !== "agent");
  const hasBothGroups = agentRows.length > 0 && otherRows.length > 0;
  const allSameNonAgentCategory =
    agentRows.length === 0 &&
    otherRows.length > 0 &&
    new Set(otherRows.map(({ task }) => classifyTaskCategory(task.kind))).size === 1;

  const toggleExpanded = (taskId: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  };

  const renderRow = ({ task, depth }: RosterTreeNode) => {
    const category = classifyTaskCategory(task.kind);
    const tone = taskStatusTone(task);
    const progress = task.native_progress;
    const title = task.description?.trim() || task.id;
    const stepText =
      progress?.last_intent?.trim() ||
      (progress?.current_tool ? resolveToolInfo(progress.current_tool).status : null) ||
      (tone === "waiting" ? "Waiting on background work" : null);
    const elapsedMs = formatTaskElapsedMs(task, nowMs, delegation);
    const isStaleWait = tone === "waiting" && elapsedMs != null && elapsedMs >= STALE_WAIT_THRESHOLD_MS;
    const tokens = progress?.tokens ?? progress?.context_tokens ?? null;
    const kindLabel =
      category === "monitor"
        ? "monitor"
        : category === "shell"
          ? "shell"
          : task.agent_type?.trim() || "agent";
    const metaParts: string[] = [];
    if (progress?.tool_count != null && progress.tool_count > 0) {
      metaParts.push(`${progress.tool_count} ${progress.tool_count === 1 ? "tool" : "tools"}`);
    }

    const showKindLabel = !allSameNonAgentCategory;
    const hasExpandableDetail = !task.session_id;
    const isDetailOpen = hasExpandableDetail && expandedIds.has(task.id);
    const hasRichMeta =
      category === "agent" ||
      Boolean(task.is_fork) ||
      Boolean(isStaleWait && elapsedMs != null) ||
      (tokens != null && tokens > 0) ||
      metaParts.length > 0;

    const rowContent = (
      <>
        <div className="session-background-roster__row-top">
          <span className={`session-background-roster__dot session-background-roster__dot--${tone}`} aria-hidden="true" />
          {depth > 0 ? <span className="session-background-roster__branch" aria-hidden="true">└─</span> : null}
          {!hasRichMeta && showKindLabel ? <span className="session-background-roster__kind">{kindLabel}</span> : null}
          <span className="session-background-roster__title" title={title}>
            {task.session_id ? (
              inRouter ? (
                <Link
                  className="session-background-roster__link"
                  to={`/timeline/${task.session_id}`}
                  aria-label={title}
                  onClick={onNavigate}
                >
                  {title}
                </Link>
              ) : (
                <a
                  className="session-background-roster__link"
                  href={`/timeline/${task.session_id}`}
                  aria-label={title}
                  onClick={onNavigate}
                >
                  {title}
                </a>
              )
            ) : (
              title
            )}
          </span>
          <div className="session-background-roster__metrics">
            {elapsedMs != null ? (
              <span className="session-background-roster__duration">{formatTurnDuration(elapsedMs)}</span>
            ) : null}
            {task.session_id ? (
              <span className="session-background-roster__ext" aria-hidden="true">↗</span>
            ) : null}
          </div>
        </div>
        {hasRichMeta && (showKindLabel || task.is_fork || (isStaleWait && elapsedMs != null) || (tokens != null && tokens > 0) || metaParts.length > 0) ? (
          <div className="session-background-roster__row-meta">
            {showKindLabel ? <span className="session-background-roster__kind">{kindLabel}</span> : null}
            {task.is_fork ? <span className="session-background-roster__badge session-background-roster__badge--fork">fork</span> : null}
            {isStaleWait && elapsedMs != null ? (
              <span className="session-background-roster__stale-wait" title="Waiting for background work">
                waiting · {formatTurnDuration(elapsedMs)}
              </span>
            ) : null}
            {tokens != null && tokens > 0 ? (
              <span className="session-background-roster__tokens">↓ {formatCompactTokens(tokens)} tok</span>
            ) : null}
            {metaParts.length > 0 ? (
              <span className="session-background-roster__meta">{metaParts.join(" · ")}</span>
            ) : null}
          </div>
        ) : null}
        {stepText ? (
          <div className="session-background-roster__row-sub">
            <span className="session-background-roster__step" title={stepText}>
              {stepText}
            </span>
          </div>
        ) : null}
      </>
    );

    return (
      <li
        key={task.id}
        className={`session-background-roster__item${task.session_id ? " is-linked" : " is-expandable"}${isDetailOpen ? " is-detail-open" : ""}`}
        data-testid="session-background-roster-item"
        data-task-kind={category}
        data-task-status={tone}
        data-spawn-depth={depth}
        style={depth > 0 ? { paddingLeft: `${depth * 14}px` } : undefined}
      >
        {hasExpandableDetail ? (
          <button
            type="button"
            className="session-background-roster__row-button"
            aria-expanded={isDetailOpen}
            onClick={() => toggleExpanded(task.id)}
          >
            {rowContent}
          </button>
        ) : (
          rowContent
        )}
      </li>
    );
  };

  return (
    <ul
      id={id}
      className="session-background-roster__list"
      data-testid="session-background-roster-list"
    >
      {hasBothGroups ? <li className="session-background-roster__section-head">Agents</li> : null}
      {agentRows.map(renderRow)}
      {hasBothGroups ? <li className="session-background-roster__section-head">Shells &amp; Monitors</li> : null}
      {otherRows.map(renderRow)}
    </ul>
  );
}

export function BackgroundTaskRoster({
  delegation,
  pendingBackgroundAgentCount,
  nowMs,
  editedFilesLabel = null,
  editedFilesTitle = null,
}: {
  delegation: SessionStateFacts["delegation"] | null | undefined;
  pendingBackgroundAgentCount?: number | null;
  nowMs: number;
  editedFilesLabel?: string | null;
  editedFilesTitle?: string | null;
}) {
  const [inlineExpanded, setInlineExpanded] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const listId = useId();
  const isMobileSheet = useMediaQuery("(max-width: 767px)");
  const supportsDialog =
    typeof HTMLDialogElement !== "undefined" &&
    typeof HTMLDialogElement.prototype?.showModal === "function";

  const activeItems = useMemo(() => delegation?.items ?? [], [delegation?.items]);
  const pendingCount = pendingBackgroundAgentCount ?? 0;

  const useSheetMode = isMobileSheet && supportsDialog;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (useSheetMode && sheetOpen) {
      if (!dialog.open && typeof dialog.showModal === "function") {
        dialog.showModal();
      }
    } else if (dialog.open && typeof dialog.close === "function") {
      dialog.close();
    }
  }, [useSheetMode, sheetOpen]);

  if (activeItems.length === 0 && pendingCount <= 0) {
    return null;
  }

  const summary = summarizeBackgroundRoster(activeItems, pendingCount);
  const canExpand = activeItems.length > 0;
  const expanded = useSheetMode ? sheetOpen : inlineExpanded;

  const handleToggle = () => {
    if (useSheetMode) {
      setSheetOpen((prev) => !prev);
    } else {
      setInlineExpanded((prev) => !prev);
    }
  };

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
          aria-controls={!useSheetMode ? listId : undefined}
          onClick={handleToggle}
        >
          <span className={`session-background-roster__chev${expanded ? " is-open" : ""}`} aria-hidden="true">
            ›
          </span>
          <span className="session-background-roster__label">{summary}</span>
          {editedFilesLabel ? (
            <span className="session-background-roster__files" title={editedFilesTitle ?? undefined}>
              {editedFilesLabel}
            </span>
          ) : null}
        </button>
      ) : (
        <div className="session-background-roster__summary session-background-roster__summary--static">
          <span className="session-background-roster__label">{summary}</span>
          {editedFilesLabel ? (
            <span className="session-background-roster__files" title={editedFilesTitle ?? undefined}>
              {editedFilesLabel}
            </span>
          ) : null}
        </div>
      )}

      {canExpand && !useSheetMode && inlineExpanded ? (
        <BackgroundTaskRosterList
          id={listId}
          delegation={delegation}
          nowMs={nowMs}
        />
      ) : null}

      {canExpand && useSheetMode ? (
        <dialog
          ref={dialogRef}
          className="session-background-sheet"
          data-testid="session-background-sheet"
          aria-label="Background work"
          onClose={() => setSheetOpen(false)}
          onCancel={() => setSheetOpen(false)}
          onClick={(event) => {
            if (event.target === dialogRef.current) setSheetOpen(false);
          }}
        >
          <div className="session-background-sheet__panel">
            <div className="session-background-sheet__grabber" aria-hidden="true" />
            <div className="session-background-sheet__head">
              <div className="session-background-sheet__title-group">
                <span className="session-background-sheet__title">Background work</span>
                <span className="session-background-sheet__summary">{summary}</span>
              </div>
              <button
                type="button"
                className="session-background-sheet__done"
                data-testid="session-background-sheet-done"
                onClick={() => setSheetOpen(false)}
              >
                Done
              </button>
            </div>
            <div className="session-background-sheet__body">
              {sheetOpen ? (
                <BackgroundTaskRosterList
                  id={listId}
                  delegation={delegation}
                  nowMs={nowMs}
                  onNavigate={() => setSheetOpen(false)}
                />
              ) : null}
            </div>
            {editedFilesLabel ? (
              <div className="session-background-sheet__footer" title={editedFilesTitle ?? undefined}>
                {editedFilesLabel}
              </div>
            ) : null}
          </div>
        </dialog>
      ) : null}
    </div>
  );
}
