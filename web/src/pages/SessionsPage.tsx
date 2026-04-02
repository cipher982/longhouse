/**
 * TimelinePage - Browse agent sessions shipped via the shipper
 *
 * Features:
 * - Timeline list grouped by day
 * - Filter by project, provider, date range (dynamic from API)
 * - Search sessions by content
 * - Live updates via timeline stream with slow reconciliation
 * - Pagination with "Load More"
 * - Click to view session details
 */

import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import { useNavigate, useSearchParams, useLocation, Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { config } from "../lib/config";
import { useAgentSessions, useAgentFilters } from "../hooks/useAgentSessions";
import { useClickOutside } from "../hooks/useClickOutside";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useDocumentVisible } from "../hooks/useDocumentVisible";
import { useEscapeKey } from "../hooks/useEscapeKey";
import { useTimelineSessionStream } from "../hooks/useTimelineSessionStream";
import { useReadinessFlag } from "../lib/readiness-contract";
import {
  type AgentSession,
  type AgentSessionFilters,
  fetchAgentSessionWorkspace,
  getTimelineCardAnchor,
  setSessionAction,
  type TimelineSessionCard,
  seedDemoSessions,
} from "../services/api/agents";
import {
  Button,
  Badge,
  Card,
  SectionHeader,
  EmptyState,
  PageShell,
  Spinner,
  Input,
} from "../components/ui";
import { PresenceBadge } from "../components/PresenceBadge";
import { parseUTC } from "../lib/dateUtils";
import { getExecutionHomeLabel } from "../lib/sessionExecutionHome";
import { resolveSessionRuntimeState } from "../lib/sessionRuntime";
import { getProviderColor, getSessionInteractionCapabilities } from "../lib/sessionWorkspace";
import { RecallPanel } from "../components/RecallPanel";
import "../styles/sessions.css";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DAYS_OPTIONS = [7, 14, 30, 60, 90] as const;
const PAGE_SIZE = 50;
const DEFAULT_DAYS_BACK = 14;
const TIMELINE_RECONCILIATION_MS = 120_000;
const DEFAULT_SORT_ORDER = "relevant";
const SESSION_WORKSPACE_PREFETCH_LIMIT = 200;

type SortOrder = "relevant" | "recent";

interface SessionsUrlState {
  project: string;
  provider: string;
  environment: string;
  hideAutonomous: boolean;
  daysBack: number;
  searchQuery: string;
  aiSearch: boolean;
  sortOrder: SortOrder;
  limit: number;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatRelativeTime(dateStr: string, nowMs: number = Date.now()): string {
  const date = parseUTC(dateStr);
  const diffMs = nowMs - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 30) return `${diffDays}d ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function getDateKey(dateStr: string, nowMs: number = Date.now()): string {
  const date = parseUTC(dateStr);
  const now = new Date(nowMs);
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const sessionDate = new Date(date.getFullYear(), date.getMonth(), date.getDate());

  if (sessionDate.getTime() === today.getTime()) return "Today";
  if (sessionDate.getTime() === yesterday.getTime()) return "Yesterday";
  return sessionDate.toLocaleDateString(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
  });
}

function useRelativeTimeClock(enabled: boolean): number {
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (!enabled) {
      return;
    }

    let intervalId: number | null = null;
    const scheduleRepeatingUpdates = () => {
      setNowMs(Date.now());
      intervalId = window.setInterval(() => {
        setNowMs(Date.now());
      }, 60_000);
    };

    setNowMs(Date.now());
    const delayUntilNextMinute = Math.max(1, 60_000 - (Date.now() % 60_000));
    const timeoutId = window.setTimeout(scheduleRepeatingUpdates, delayUntilNextMinute);

    return () => {
      window.clearTimeout(timeoutId);
      if (intervalId !== null) {
        window.clearInterval(intervalId);
      }
    };
  }, [enabled]);

  return nowMs;
}

function groupThreadCardsByDay(
  cards: TimelineSessionCard[],
  nowMs: number,
): Map<string, TimelineSessionCard[]> {
  const groups = new Map<string, TimelineSessionCard[]>();

  for (const card of cards) {
    const key = getDateKey(getTimelineCardAnchor(card), nowMs);
    const existing = groups.get(key) || [];
    existing.push(card);
    groups.set(key, existing);
  }

  return groups;
}

function buildSessionDetailPath(
  session: Pick<AgentSession, "id" | "provider" | "match_event_id">,
  matchEventId?: number | null,
): string {
  const params = new URLSearchParams();
  if (matchEventId != null) {
    params.set("event_id", String(matchEventId));
  }
  const search = params.toString();
  return `/timeline/${session.id}${search ? `?${search}` : ""}`;
}

function ProviderIcon({ provider }: { provider: string }) {
  const color = getProviderColor(provider);
  const svgProps = { width: 14, height: 14, viewBox: "0 0 24 24", fill: "none", "aria-hidden": true as const, style: { color, flexShrink: 0 } };

  switch (provider) {
    case "claude":
      return (
        <svg {...svgProps}>
          <path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" fill="currentColor" />
        </svg>
      );
    case "codex":
      return (
        <svg {...svgProps}>
          <path d="M12 2a2.5 2.5 0 010 5 2.5 2.5 0 01-4.33 2.5A2.5 2.5 0 012 12a2.5 2.5 0 015.67 2.5A2.5 2.5 0 0112 17a2.5 2.5 0 014.33 2.5A2.5 2.5 0 0122 12a2.5 2.5 0 01-5.67-2.5A2.5 2.5 0 0112 7a2.5 2.5 0 010-5z" fill="currentColor" opacity="0.9" />
        </svg>
      );
    case "gemini":
      return (
        <svg {...svgProps}>
          <path d="M12 2C12 10 14 12 22 12C14 12 12 14 12 22C12 14 10 12 2 12C10 12 12 10 12 2Z" fill="currentColor" />
        </svg>
      );
    case "zai":
      return (
        <svg {...svgProps}>
          <path d="M6 6h12v2.5L8.5 18H18v2H6v-2.5L15.5 8H6z" fill="currentColor" />
        </svg>
      );
    default:
      return (
        <svg {...svgProps} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="4 17 10 11 4 5" />
          <line x1="12" y1="19" x2="20" y2="19" />
        </svg>
      );
  }
}

function getTurnsColor(turns: number): string | undefined {
  if (turns < 5) return undefined;
  if (turns < 15) return "var(--color-brand-primary)";
  if (turns < 30) return "var(--color-brand-accent)";
  return "var(--color-intent-error)";
}

function isValidTitle(name: string | null | undefined): name is string {
  if (!name) return false;
  if (name.length < 3) return false;
  if (name.startsWith("tmp")) return false;
  // Skip git hashes and hex IDs (only hex chars 0-9a-f, 8+ chars)
  // Uses [0-9a-f] not [a-z0-9] to avoid suppressing real names like "longhouse"
  if (/^[0-9a-f]{8,}$/i.test(name)) return false;
  return true;
}

/** Primary identifier: what repo/project/directory is this session for? */
function getProjectLabel(session: AgentSession): string {
  if (isValidTitle(session.project)) return session.project;
  if (session.cwd) {
    const folder = session.cwd.split("/").pop();
    if (folder && folder.length >= 2) return folder;
  }
  if (session.git_repo) {
    const name = session.git_repo.replace(/\.git$/, "").split("/").pop();
    if (name) return name;
  }
  return session.provider;
}

/** Secondary: what was done in this session? */
function getSessionTitle(session: AgentSession): string {
  if (session.summary_title && session.summary_title !== "Untitled Session") {
    return session.summary_title;
  }
  if (session.first_user_message) {
    const snippet = session.first_user_message.trim().slice(0, 80);
    if (snippet) return snippet;
  }
  if (isValidTitle(session.git_branch)) return session.git_branch!;
  return "";
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function renderHighlightedText(text: string, query: string) {
  const tokens = query.split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return text;
  const pattern = tokens.map(escapeRegExp).join("|");
  if (!pattern) return text;
  const splitRegex = new RegExp(`(${pattern})`, "gi");
  const matchRegex = new RegExp(`^(${pattern})$`, "i");

  return text.split(splitRegex).map((part, idx) =>
    matchRegex.test(part) ? (
      <mark key={`${idx}-${part}`} className="search-highlight">
        {part}
      </mark>
    ) : (
      part
    )
  );
}


function getRuntimeMetaLabel(runtime: ReturnType<typeof resolveSessionRuntimeState>): string | null {
  if (runtime.truthTier === "managed-local") {
    return "Local runtime";
  }
  if (runtime.lastLiveAt) {
    if (runtime.truthTier === "stale" || runtime.confidence === "stale") {
      return `Seen ${formatRelativeTime(runtime.lastLiveAt)}`;
    }
  }
  return null;
}

function parsePositiveIntParam(rawValue: string | null, fallback: number, min: number = 1): number {
  if (rawValue == null || rawValue.trim() === "") return fallback;
  const parsed = Number(rawValue);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.floor(parsed));
}

function readSessionsUrlState(searchParams: URLSearchParams): SessionsUrlState {
  const mode = searchParams.get("mode");
  const aiSearch =
    mode === "hybrid" ||
    mode === "semantic" ||
    mode === "smart" ||
    searchParams.get("semantic") === "1";

  return {
    project: searchParams.get("project") || "",
    provider: searchParams.get("provider") || "",
    environment: searchParams.get("environment") || "",
    hideAutonomous: searchParams.get("hide_autonomous") !== "false",
    daysBack: parsePositiveIntParam(searchParams.get("days_back"), DEFAULT_DAYS_BACK),
    searchQuery: searchParams.get("query") || "",
    aiSearch,
    sortOrder: searchParams.get("sort") === "recent" ? "recent" : DEFAULT_SORT_ORDER,
    limit: parsePositiveIntParam(searchParams.get("limit"), PAGE_SIZE, PAGE_SIZE),
  };
}

function buildSessionsSearchParams(state: SessionsUrlState): URLSearchParams {
  const params = new URLSearchParams();

  if (state.project) params.set("project", state.project);
  if (state.provider) params.set("provider", state.provider);
  if (state.environment) params.set("environment", state.environment);
  if (state.daysBack !== DEFAULT_DAYS_BACK) params.set("days_back", String(state.daysBack));
  if (state.searchQuery) params.set("query", state.searchQuery);
  if (state.aiSearch) params.set("mode", "hybrid");
  if (state.searchQuery && state.sortOrder !== DEFAULT_SORT_ORDER) params.set("sort", state.sortOrder);
  if (!state.hideAutonomous) params.set("hide_autonomous", "false");
  if (state.limit !== PAGE_SIZE) params.set("limit", String(state.limit));

  return params;
}

// ---------------------------------------------------------------------------
// Filter Components
// ---------------------------------------------------------------------------

function FilterChip({ label, onDismiss }: { label: string; onDismiss: () => void }) {
  return (
    <div className="sessions-filter-chip">
      <span className="sessions-filter-chip-label">{label}</span>
      <button
        type="button"
        className="sessions-filter-chip-dismiss"
        onClick={onDismiss}
        aria-label={`Remove ${label} filter`}
      >
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </div>
  );
}

function FilterSection({
  label,
  value,
  options,
  onChange,
  loading,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
  loading?: boolean;
}) {
  if (options.length === 0 && !loading) return null;
  return (
    <div className="filter-section" data-filter-section={label.toLowerCase()}>
      <div className="filter-section-label">{label}</div>
      <div className="filter-section-options">
        <button
          type="button"
          className={`filter-option-btn${!value ? " filter-option-btn--active" : ""}`}
          onClick={() => onChange("")}
        >
          All
        </button>
        {options.map((opt) => (
          <button
            key={opt}
            type="button"
            className={`filter-option-btn${value === opt ? " filter-option-btn--active" : ""}`}
            onClick={() => onChange(opt)}
            data-filter-option={opt}
          >
            {opt}
          </button>
        ))}
      </div>
    </div>
  );
}

function DaysSection({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <div className="filter-section" data-filter-section="time">
      <div className="filter-section-label">Time window</div>
      <div className="filter-section-options">
        {DAYS_OPTIONS.map((days) => (
          <button
            key={days}
            type="button"
            className={`filter-option-btn${value === days ? " filter-option-btn--active" : ""}`}
            onClick={() => onChange(days)}
            data-filter-option={`${days}d`}
          >
            {days}d
          </button>
        ))}
      </div>
    </div>
  );
}

interface FilterPopoverProps {
  anchorRef: React.RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  project: string; setProject: (v: string) => void; projectOptions: string[];
  provider: string; setProvider: (v: string) => void; providerOptions: string[];
  environment: string; setEnvironment: (v: string) => void; machineOptions: string[];
  daysBack: number; setDaysBack: (v: number) => void;
  hideAutonomous: boolean; setHideAutonomous: (v: boolean) => void;
  filtersLoading: boolean;
}

function FilterPopover({
  anchorRef, onClose,
  project, setProject, projectOptions,
  provider, setProvider, providerOptions,
  environment, setEnvironment, machineOptions,
  daysBack, setDaysBack,
  hideAutonomous, setHideAutonomous,
  filtersLoading,
}: FilterPopoverProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);

  useClickOutside({
    refs: [ref, anchorRef],
    onClickOutside: onClose,
  });
  useEscapeKey(() => {
    onClose();
  });

  useEffect(() => {
    if (!anchorRef.current) return;
    const rect = anchorRef.current.getBoundingClientRect();
    setPos({ top: rect.bottom + 8, right: window.innerWidth - rect.right });
  }, [anchorRef]);

  if (!pos) return null;

  return (
    <div
      ref={ref}
      id="filter-panel"
      role="dialog"
      aria-label="Session filters"
      className="sessions-filter-popover"
      style={{ top: pos.top, right: pos.right }}
    >
      <FilterSection label="Provider" value={provider} options={providerOptions} onChange={setProvider} loading={filtersLoading} />
      <FilterSection label="Machine" value={environment} options={machineOptions} onChange={setEnvironment} loading={filtersLoading} />
      <FilterSection label="Project" value={project} options={projectOptions} onChange={setProject} loading={filtersLoading} />
      <DaysSection value={daysBack} onChange={setDaysBack} />
      <label className="sessions-filter-toggle-label">
        <input
          type="checkbox"
          checked={!hideAutonomous}
          onChange={(e) => setHideAutonomous(!e.target.checked)}
        />
        show autonomous
      </label>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Session Card Component
// ---------------------------------------------------------------------------

interface SessionCardProps {
  thread: TimelineSessionCard;
  onClick: () => void;
  onPrefetch?: () => void;
  onArchive?: () => void;
  highlightQuery?: string;
  isSemanticResult?: boolean;
  compatibilityMode?: boolean;
  relativeNowMs: number;
}

function SessionCard({
  thread,
  onClick,
  onPrefetch,
  onArchive,
  highlightQuery,
  isSemanticResult,
  compatibilityMode = false,
  relativeNowMs,
}: SessionCardProps) {
  const [confirming, setConfirming] = useState(false);
  const detailSession = thread.detail;
  const session = compatibilityMode ? detailSession : thread.head;
  const turnCount = session.user_messages;
  const toolCount = session.tool_calls;
  const runtime = resolveSessionRuntimeState(session);
  const runtimeMetaLabel = getRuntimeMetaLabel(runtime);

  const projectLabel = getProjectLabel(session);
  const title = getSessionTitle(session);
  const executionHomeLabel = getExecutionHomeLabel(session.execution_home);
  const showHeadOriginLabel =
    !compatibilityMode && !!thread.head_origin_label && thread.head_origin_label !== executionHomeLabel;

  const showKeywordSnippet = !isSemanticResult && !!highlightQuery && !!detailSession.match_snippet;
  const showSemanticSnippet = isSemanticResult && !!detailSession.match_snippet;
  const showSummary = !showKeywordSnippet && !showSemanticSnippet && !!session.summary;
  const showGenerating = !showKeywordSnippet && !showSemanticSnippet && !session.summary && !session.summary_title;
  const primaryActionLabel = compatibilityMode
    ? "Open match"
    : getSessionInteractionCapabilities({ session }).primaryActionLabel;
  const cardClassName = [
    "session-card",
    confirming ? "session-card--confirming" : "",
    runtime.isExecuting ? "session-card--live" : "",
    runtime.isIdle ? "session-card--idle" : "",
    runtime.tone === "inferred" ? "session-card--inferred" : "",
    runtime.tone === "thinking" ? "session-card--thinking" : "",
    runtime.tone === "running" ? "session-card--running" : "",
    runtime.tone === "needs-user" ? "session-card--needs-user" : "",
    runtime.tone === "blocked" ? "session-card--blocked" : "",
  ].filter(Boolean).join(" ");

  return (
    <Card
      className={cardClassName}
      onMouseEnter={onPrefetch}
      onFocus={onPrefetch}
      onPointerDown={(event) => {
        if (event.pointerType === "touch" || event.pointerType === "pen") {
          onPrefetch?.();
        }
      }}
      style={{ borderLeftColor: getProviderColor(session.provider) }}
      data-testid="session-card"
      data-session-id={detailSession.id}
      data-thread-id={thread.thread_id}
      data-runtime-tone={runtime.tone}
      data-execution-home={session.execution_home || "legacy"}
    >
      {!confirming && (
        <button
          type="button"
          className="session-card-hitbox"
          onClick={onClick}
          aria-label={title ? `${primaryActionLabel}: ${title}` : primaryActionLabel}
        />
      )}

      <div className={`session-card-content${confirming ? "" : " session-card-content--passthrough"}`}>
        <div className="session-card-header">
          <div className="session-card-project">{projectLabel}</div>
          <div
            className={
              onArchive && !confirming
                ? "session-card-header-right session-card-header-right--with-archive"
                : "session-card-header-right"
            }
          >
            <span className="session-card-time">{formatRelativeTime(getTimelineCardAnchor(thread), relativeNowMs)}</span>
          </div>
        </div>

        <div className="session-card-meta">
          <span className="session-card-provider-badge">
            <ProviderIcon provider={session.provider} />
            <span className="provider-name" style={{ color: getProviderColor(session.provider) }}>{session.provider}</span>
          </span>
          {session.git_branch && (
            <span className="session-card-branch-badge">
              <span className="branch-icon">&#x2387;</span>
              {session.git_branch}
            </span>
          )}
          {executionHomeLabel && (
            <span
              className={
                executionHomeLabel === "Legacy"
                  ? "environment-badge environment-badge--secondary"
                  : "environment-badge"
              }
            >
              {executionHomeLabel}
            </span>
          )}
          {showHeadOriginLabel && (
            <span className="environment-badge environment-badge--secondary">Head: {thread.head_origin_label}</span>
          )}
          {!compatibilityMode &&
            thread.continuation_count > 1 &&
            thread.started_origin_label &&
            thread.started_origin_label !== thread.head_origin_label && (
            <span className="environment-badge environment-badge--secondary">Started: {thread.started_origin_label}</span>
          )}
          {!compatibilityMode && thread.continuation_count > 1 && (
            <span className="environment-badge environment-badge--secondary">
              {thread.continuation_count} continuations
            </span>
          )}
        </div>

        <div className="session-card-body">
          {runtime.hasSignal && (
            <div className={`session-card-runtime session-card-runtime--${runtime.tone}`}>
              <PresenceBadge
                state={runtime.presenceState}
                tool={runtime.presenceTool}
                compact
                heuristicActive={runtime.heuristicActive}
                showUnknown={runtime.truthTier === "stale"}
              />
              <span className="session-card-runtime-phase">{runtime.displayPhase}</span>
              {runtimeMetaLabel && (
                <span className="session-card-runtime-meta">{runtimeMetaLabel}</span>
              )}
            </div>
          )}
          {title && <div className="session-card-title">{title}</div>}
          {showSummary && (
            <div className="session-card-summary">{session.summary}</div>
          )}
          {showGenerating && (
            <div className="session-card-summary session-card-summary--pending">
              Generating summary<span className="session-card-dots" aria-hidden="true" />
            </div>
          )}
          {showKeywordSnippet && (
            <div className="session-card-snippet">
              {renderHighlightedText(detailSession.match_snippet!, highlightQuery!)}
            </div>
          )}
          {showSemanticSnippet && (
            <div className="session-card-snippet session-card-snippet--ai">
              {detailSession.match_snippet}
            </div>
          )}
          {isSemanticResult && detailSession.match_score != null && detailSession.match_score >= 0.5 && (
            <div className="session-card-score" title={`Semantic similarity: ${Math.round(detailSession.match_score * 100)}%`}>
              {Math.round(detailSession.match_score * 100)}% match
            </div>
          )}
        </div>

        {!confirming && (
          <div className="session-card-footer">
            <div className="session-card-stats">
              <div className="session-card-stats-primary">
                <span className="session-stat" style={{ color: getTurnsColor(turnCount) }}>{turnCount} {turnCount === 1 ? 'turn' : 'turns'}</span>
                <span className="session-stat-separator">&middot;</span>
                <span className="session-stat">{toolCount} {toolCount === 1 ? 'tool' : 'tools'}</span>
              </div>
              <div className="session-card-stats-secondary">
                <span className="session-stat session-stat--secondary">
                  {compatibilityMode
                    ? `Matched ${formatRelativeTime(detailSession.started_at, relativeNowMs)}`
                    : `Started ${formatRelativeTime(thread.root.started_at, relativeNowMs)}`}
                </span>
              </div>
            </div>
            <div className="session-card-actions">
              <span className="session-card-action-label">{primaryActionLabel}</span>
              <span className="session-card-arrow">&rarr;</span>
            </div>
          </div>
        )}
      </div>

      {onArchive && !confirming && (
        <button
          type="button"
          className="session-card-archive-btn"
          onClick={() => setConfirming(true)}
          aria-label="Archive session"
          title="Archive"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <polyline points="3 6 5 6 21 6" />
            <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
            <path d="M10 11v6M14 11v6" />
            <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
          </svg>
        </button>
      )}

      {confirming && (
        <div className="session-card-confirm-row">
          <span className="session-card-confirm-label">Archive this session?</span>
          <button
            type="button"
            className="session-card-confirm-cancel"
            onClick={() => setConfirming(false)}
          >
            Cancel
          </button>
          <button
            type="button"
            className="session-card-confirm-ok"
            onClick={() => { setConfirming(false); onArchive?.(); }}
          >
            Archive
          </button>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Session Group Component
// ---------------------------------------------------------------------------

interface SessionGroupProps {
  title: string;
  sessions: TimelineSessionCard[];
  onSessionClick: (thread: TimelineSessionCard) => void;
  onSessionPrefetch: (thread: TimelineSessionCard) => void;
  onSessionArchive: (thread: TimelineSessionCard) => void;
  highlightQuery?: string;
  isSemanticResult?: boolean;
  compatibilityMode?: boolean;
  relativeNowMs: number;
}

function SessionGroup({
  title,
  sessions,
  onSessionClick,
  onSessionPrefetch,
  onSessionArchive,
  highlightQuery,
  isSemanticResult,
  compatibilityMode,
  relativeNowMs,
}: SessionGroupProps) {
  return (
    <div className="session-group">
      <div className="session-group-header">
        <span className="session-group-title">{title}</span>
        <Badge variant="neutral" className="session-group-count">{sessions.length}</Badge>
      </div>
      <div className="session-group-list">
        {sessions.map((thread) => (
          <SessionCard
            key={thread.thread_id}
            thread={thread}
            onClick={() => onSessionClick(thread)}
            onPrefetch={() => onSessionPrefetch(thread)}
            onArchive={() => onSessionArchive(thread)}
            highlightQuery={highlightQuery}
            isSemanticResult={isSemanticResult}
            compatibilityMode={compatibilityMode}
            relativeNowMs={relativeNowMs}
          />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export default function SessionsPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const urlState = useMemo(() => readSessionsUrlState(searchParams), [searchParams]);
  const {
    project,
    provider,
    environment,
    hideAutonomous,
    daysBack,
    searchQuery,
    aiSearch,
    sortOrder,
    limit,
  } = urlState;

  const updateUrlState = useCallback(
    (updater: SessionsUrlState | Partial<SessionsUrlState> | ((prev: SessionsUrlState) => SessionsUrlState)) => {
      const previous = readSessionsUrlState(searchParams);
      const next =
        typeof updater === "function"
          ? updater(previous)
          : {
              ...previous,
              ...updater,
            };
      setSearchParams(buildSessionsSearchParams(next), { replace: true });
    },
    [searchParams, setSearchParams]
  );

  const updateFilterState = useCallback(
    (patch: Partial<SessionsUrlState> | ((prev: SessionsUrlState) => SessionsUrlState)) => {
      updateUrlState((previous) => {
        const next =
          typeof patch === "function"
            ? patch(previous)
            : {
                ...previous,
                ...patch,
              };
        return {
          ...next,
          limit: PAGE_SIZE,
        };
      });
    },
    [updateUrlState]
  );

  const [popoverOpen, setPopoverOpen] = useState(false);
  const filterBtnRef = useRef<HTMLButtonElement>(null);
  const [recallOpen, setRecallOpen] = useState(false);
  const queryClient = useQueryClient();
  const prefetchedSessionIdsRef = useRef<Set<string>>(new Set());

  // Fetch dynamic filter options
  const { data: filtersData, isLoading: filtersLoading } = useAgentFilters(daysBack, popoverOpen);
  const projectOptions = filtersData?.projects || [];
  const providerOptions = filtersData?.providers || [];
  const machineOptions = filtersData?.machines || [];
  const handleProjectChange = useCallback(
    (value: string) => updateFilterState({ project: value }),
    [updateFilterState]
  );
  const handleProviderChange = useCallback(
    (value: string) => updateFilterState({ provider: value }),
    [updateFilterState]
  );
  const handleEnvironmentChange = useCallback(
    (value: string) => updateFilterState({ environment: value }),
    [updateFilterState]
  );
  const handleDaysBackChange = useCallback(
    (value: number) => updateFilterState({ daysBack: value }),
    [updateFilterState]
  );
  const handleHideAutonomousChange = useCallback(
    (value: boolean) => updateFilterState({ hideAutonomous: value }),
    [updateFilterState]
  );
  const handleSearchQueryChange = useCallback(
    (value: string) => {
      updateFilterState((previous) => ({
        ...previous,
        searchQuery: value,
        sortOrder: value ? previous.sortOrder : DEFAULT_SORT_ORDER,
      }));
    },
    [updateFilterState]
  );
  const handleAiSearchToggle = useCallback(
    () => updateFilterState((previous) => ({ ...previous, aiSearch: !previous.aiSearch })),
    [updateFilterState]
  );
  const handleSortOrderChange = useCallback(
    (value: SortOrder) => updateFilterState({ sortOrder: value }),
    [updateFilterState]
  );

  const documentVisible = useDocumentVisible();
  const relativeNowMs = useRelativeTimeClock(documentVisible);
  const debouncedQuery = useDebouncedValue(searchQuery, aiSearch ? 700 : 300);
  const aiSearchPending = aiSearch && searchQuery !== debouncedQuery;

  // Build filters — mode and sort are passed through to the backend.
  // Hybrid mode sends a single request; the backend handles RRF fusion.
  const filters: AgentSessionFilters = useMemo(
    () => ({
      project: project || undefined,
      provider: provider || undefined,
      environment: environment || undefined,
      days_back: daysBack,
      query: debouncedQuery || undefined,
      limit,
      mode: aiSearch ? "hybrid" : undefined,
      sort: debouncedQuery ? (sortOrder === "recent" ? "recency" : "relevance") : undefined,
      hide_autonomous: hideAutonomous ? undefined : false,
    }),
    [project, provider, environment, daysBack, debouncedQuery, limit, aiSearch, sortOrder, hideAutonomous]
  );

  const timelineStreamEligible = !debouncedQuery && !aiSearch && typeof EventSource !== "undefined";
  const timelineStreamBootstrapKeysRef = useRef<Set<string>>(new Set());
  const timelineStreamBootstrapKey = useMemo(() => JSON.stringify(filters), [filters]);

  // Single unified query — use SSE for live rows and keep a slow polling backstop.
  const timelineResult = useAgentSessions(filters, {
    refetchInterval: timelineStreamEligible
      ? TIMELINE_RECONCILIATION_MS
      : (query) => {
          const pendingSessions = query.state.data?.sessions?.some(
            (session) => !session.head.summary_title && !session.head.summary
          );
          return pendingSessions ? 3_000 : 30_000;
        },
  });
  const data = timelineResult.data;
  const isLoading = timelineResult.isLoading;
  const error = timelineResult.error;
  const refetch = timelineResult.refetch;
  const timelineStreamEnabled = timelineStreamEligible && documentVisible && !isLoading && !!data;
  const skipInitialTimelineReplay =
    timelineStreamEnabled && !timelineStreamBootstrapKeysRef.current.has(timelineStreamBootstrapKey);

  useEffect(() => {
    if (!timelineStreamEnabled) {
      return;
    }
    timelineStreamBootstrapKeysRef.current.add(timelineStreamBootstrapKey);
  }, [timelineStreamBootstrapKey, timelineStreamEnabled]);

  const sessions = useMemo(() => data?.sessions || [], [data?.sessions]);
  const total = data?.total || 0;
  const hasRealSessions = data?.has_real_sessions ?? true;
  const compatibilityMode = data?.compatibility_mode === "query_grouped";
  const hasMore = compatibilityMode ? (data?.compatibility_has_more ?? false) : sessions.length < total;

  useTimelineSessionStream(filters, {
    enabled: timelineStreamEnabled,
    skipInitialReplay: skipInitialTimelineReplay,
  });

  const threadCards = sessions;
  const groupedSessions = useMemo(() => groupThreadCardsByDay(threadCards, relativeNowMs), [threadCards, relativeNowMs]);

  const prefetchSessionWorkspace = useCallback((sessionId: string | null) => {
    if (!sessionId || prefetchedSessionIdsRef.current.has(sessionId)) {
      return;
    }

    prefetchedSessionIdsRef.current.add(sessionId);
    void queryClient
      .prefetchQuery({
        queryKey: [
          "agent-session-workspace",
          sessionId,
          { limit: SESSION_WORKSPACE_PREFETCH_LIMIT, branch_mode: "head" as const },
        ],
        queryFn: () =>
          fetchAgentSessionWorkspace(sessionId, {
            limit: SESSION_WORKSPACE_PREFETCH_LIMIT,
            branch_mode: "head",
          }),
        staleTime: 10_000,
      })
      .catch(() => {
        prefetchedSessionIdsRef.current.delete(sessionId);
      });
  }, [queryClient]);

  const handleSessionPrefetch = useCallback((thread: TimelineSessionCard) => {
    prefetchSessionWorkspace(thread.detail.id);
  }, [prefetchSessionWorkspace]);

  const headerActions = (
    <div className="sessions-header-actions">
      {threadCards.length > 0 && (
        <span className="sessions-header-count">
          {compatibilityMode ? `${threadCards.length} results` : `${threadCards.length} tasks`}
        </span>
      )}
      <Button
        variant="ghost"
        size="sm"
        onClick={() => navigate("/briefings")}
      >
        Briefings
      </Button>
    </div>
  );

  // Archive a session — optimistic remove, confirmed by inline card UI
  const handleSessionArchive = useCallback(async (thread: TimelineSessionCard) => {
    const sessionId = thread.detail.id;
    queryClient.setQueriesData<{ sessions: TimelineSessionCard[]; total: number }>(
      { queryKey: ["agent-sessions"] },
      (old) => {
        if (!old) return old;
        return {
          ...old,
          sessions: old.sessions.filter((t) => t.thread_id !== thread.thread_id),
          total: Math.max(0, old.total - 1),
        };
      },
    );
    try {
      await setSessionAction(sessionId, "archive");
    } catch {
      queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
      toast.error("Failed to archive session");
    }
  }, [queryClient]);

  // Handle session click - preserve current filters in location state
  const handleSessionClick = useCallback((thread: TimelineSessionCard) => {
    const detailSession = thread.detail;
    const matchEventId = debouncedQuery ? detailSession.match_event_id : null;
    navigate(buildSessionDetailPath(detailSession, matchEventId), {
      state: { from: location.pathname + location.search },
    });
  }, [navigate, location, debouncedQuery]);

  // Load more sessions
  const handleLoadMore = useCallback(() => {
    updateUrlState((previous) => ({
      ...previous,
      limit: previous.limit + PAGE_SIZE,
    }));
  }, [updateUrlState]);

  // Clear filters
  const handleClearFilters = useCallback(() => {
    updateUrlState({
      project: "",
      provider: "",
      environment: "",
      hideAutonomous: true,
      daysBack: DEFAULT_DAYS_BACK,
      searchQuery: "",
      aiSearch: false,
      sortOrder: DEFAULT_SORT_ORDER,
      limit: PAGE_SIZE,
    });
    setPopoverOpen(false);
  }, [updateUrlState]);

  // Demo seeding state
  const [demoLoading, setDemoLoading] = useState(false);
  const [seedError, setSeedError] = useState<string | null>(null);

  const handleSeedDemo = useCallback(async () => {
    setDemoLoading(true);
    setSeedError(null);
    try {
      const result = await seedDemoSessions();
      // Invalidate both sessions and filter options so new demo data appears
      queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
      queryClient.invalidateQueries({ queryKey: ["agent-session-filters"] });
      if (result.sessions_failed > 0) {
        setSeedError(`Loaded ${result.sessions_created} demo sessions, ${result.sessions_failed} failed. Check backend logs.`);
      }
    } catch {
      setSeedError("Failed to load demo sessions. Please try again.");
    } finally {
      setDemoLoading(false);
    }
  }, [queryClient]);

  const hasFilters = !!(project || provider || environment || daysBack !== DEFAULT_DAYS_BACK || searchQuery);
  const showGuidedEmptyState = sessions.length === 0 && !hasFilters;

  // Count active non-default filters (for badge)
  const activeFilterCount = [
    project,
    provider,
    environment,
    daysBack !== DEFAULT_DAYS_BACK ? "active" : "",
    !hideAutonomous ? "active" : "",
  ].filter(Boolean).length;

  // Ready signal for E2E
  useReadinessFlag({ ready: !isLoading, screenshotReady: !isLoading });

  // Loading state
  if (isLoading && sessions.length === 0) {
    return (
      <PageShell size="wide" className="sessions-page-container">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading timeline..."
          description="Fetching your timeline sessions."
        />
      </PageShell>
    );
  }

  // Error state — full-page only when there's no cached data to fall back on
  if (error && sessions.length === 0) {
    return (
      <PageShell size="wide" className="sessions-page-container">
        <EmptyState
          variant="error"
          title="Error loading timeline"
          description={error instanceof Error ? error.message : "Unknown error"}
          action={
            <Button variant="primary" onClick={() => refetch()}>
              Try Again
            </Button>
          }
        />
      </PageShell>
    );
  }

  // Hero empty state — no sessions, no filters: show full-viewport centered CTA
  if (showGuidedEmptyState) {
    return (
      <PageShell size="wide" className="sessions-page-container">
        <div className="sessions-hero-empty">
          <EmptyState
            title="Welcome to Longhouse"
            description="Your AI coding sessions from Claude Code, Codex, and Gemini will appear here as a searchable timeline."
            action={
              <div className="sessions-guided-actions">
                <Button
                  variant="primary"
                  size="md"
                  onClick={handleSeedDemo}
                  disabled={demoLoading}
                >
                  {demoLoading ? "Loading..." : "Load demo sessions"}
                </Button>
                {seedError && (
                  <p style={{ color: "var(--color-intent-error)", marginTop: "0.5rem", fontSize: "0.875rem" }}>
                    {seedError}
                  </p>
                )}
              </div>
            }
          />
          <div className="sessions-guided-steps">
            <p className="sessions-guided-steps-label">To start shipping your own sessions:</p>
            <ol className="sessions-guided-steps-list">
              <li><code>longhouse connect</code> &mdash; link your CLI tools</li>
              <li>Use Claude Code, Codex, or Gemini as normal</li>
              <li>Sessions appear here automatically</li>
            </ol>
            <p className="sessions-guided-cli-hint">
              Don&apos;t have a CLI yet? Longhouse supports{" "}
              <a href="https://docs.anthropic.com/en/docs/claude-code/overview" target="_blank" rel="noopener noreferrer">Claude Code</a>,{" "}
              <a href="https://github.com/openai/codex" target="_blank" rel="noopener noreferrer">Codex CLI</a>, and{" "}
              <a href="https://github.com/google-gemini/gemini-cli" target="_blank" rel="noopener noreferrer">Gemini CLI</a>.
            </p>
          </div>
        </div>
      </PageShell>
    );
  }

  return (
    <PageShell size="wide" className="sessions-page-container">
      <div className="sessions-page">
        <SectionHeader
          title="Timeline"
          actions={headerActions}
        />

        {error && sessions.length > 0 && (
          <div className="sessions-stale-banner" role="status">
            <span>Unable to refresh — showing cached data.</span>
            <button type="button" className="sessions-stale-retry" onClick={() => refetch()}>
              Retry
            </button>
          </div>
        )}

        {!hasRealSessions && sessions.length > 0 && !hasFilters && (
          <div className="sessions-demo-banner">
            <span>These are demo sessions.</span>{" "}
            <span>
              Start Claude Code, Codex, or Gemini — your real sessions will appear here automatically.
            </span>
          </div>
        )}

        {!config.llmAvailable && sessions.length > 0 && (
          <div className="sessions-llm-hint">
            Session summaries require an LLM provider.{" "}
            <Link to="/settings">Configure in Settings</Link>
          </div>
        )}

        {/* Compact Toolbar */}
        <div className="sessions-toolbar">
          <div className="sessions-search-row">
            <Input
              type="search"
              placeholder="Search sessions..."
              value={searchQuery}
              onChange={(e) => handleSearchQueryChange(e.target.value)}
              className="sessions-search-input"
            />
            <button
              type="button"
              className={`sessions-ai-toggle${aiSearch ? " sessions-ai-toggle--active" : ""}`}
              onClick={handleAiSearchToggle}
              aria-pressed={aiSearch}
              title={aiSearch ? "AI search on — finds by meaning (slower)" : "AI search — finds sessions by meaning"}
            >
              {aiSearch && (isLoading || aiSearchPending) ? (
                <Spinner size="sm" />
              ) : (
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z" />
                  <path d="M20 3v4" />
                  <path d="M22 5h-4" />
                  <path d="M4 17v2" />
                  <path d="M5 18H3" />
                </svg>
              )}
              <span className="sessions-ai-toggle-label">AI</span>
            </button>
            {debouncedQuery && (
              <div
                className="sessions-search-mode sessions-sort-toggle"
                role="radiogroup"
                aria-label="Sort order"
                onKeyDown={(e) => {
                  const orders: Array<"relevant" | "recent"> = ["relevant", "recent"];
                  const idx = orders.indexOf(sortOrder);
                  if (e.key === "ArrowLeft") { e.preventDefault(); handleSortOrderChange(orders[(idx + 1) % 2]); }
                  if (e.key === "ArrowRight") { e.preventDefault(); handleSortOrderChange(orders[(idx + 1) % 2]); }
                  requestAnimationFrame(() => {
                    const active = e.currentTarget.querySelector<HTMLButtonElement>('[aria-checked="true"]');
                    active?.focus();
                  });
                }}
              >
                <button
                  type="button"
                  role="radio"
                  aria-checked={sortOrder === "relevant"}
                  tabIndex={sortOrder === "relevant" ? 0 : -1}
                  className={`sessions-mode-btn${sortOrder === "relevant" ? " sessions-mode-btn--active" : ""}`}
                  onClick={() => handleSortOrderChange("relevant")}
                  title="Sort by relevance to your query"
                >
                  Relevant
                </button>
                <button
                  type="button"
                  role="radio"
                  aria-checked={sortOrder === "recent"}
                  tabIndex={sortOrder === "recent" ? 0 : -1}
                  className={`sessions-mode-btn${sortOrder === "recent" ? " sessions-mode-btn--active" : ""}`}
                  onClick={() => handleSortOrderChange("recent")}
                  title="Sort by most recent activity"
                >
                  Recent
                </button>
              </div>
            )}
          </div>

          {/* Active filter chips */}
          {(provider || environment || project || daysBack !== DEFAULT_DAYS_BACK || !hideAutonomous) && (
            <div className="sessions-filter-chips">
              {provider && <FilterChip label={provider} onDismiss={() => handleProviderChange("")} />}
              {environment && <FilterChip label={environment} onDismiss={() => handleEnvironmentChange("")} />}
              {project && <FilterChip label={project} onDismiss={() => handleProjectChange("")} />}
              {daysBack !== DEFAULT_DAYS_BACK && <FilterChip label={`${daysBack}d`} onDismiss={() => handleDaysBackChange(DEFAULT_DAYS_BACK)} />}
              {!hideAutonomous && <FilterChip label="show auto" onDismiss={() => handleHideAutonomousChange(true)} />}
            </div>
          )}

          <div className="sessions-toolbar-actions">
            <Button variant="ghost" size="sm" onClick={handleClearFilters} disabled={!hasFilters}>
              Clear
            </Button>
            <button
              type="button"
              className={`sessions-filter-toggle sessions-recall-toggle${recallOpen ? " sessions-filter-toggle--open" : ""}`}
              onClick={() => setRecallOpen((v) => !v)}
              aria-expanded={recallOpen}
              aria-controls="recall-panel"
              aria-label="Recall — search conversation turns by meaning"
              data-testid="recall-toggle"
              title="Search inside conversations by meaning"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="11" cy="11" r="8" />
                <path d="M11 8v3l2 2" />
                <line x1="21" y1="21" x2="16.65" y2="16.65" />
              </svg>
              <span>Recall</span>
            </button>
            <button
              ref={filterBtnRef}
              type="button"
              className={`sessions-filter-toggle${popoverOpen ? " sessions-filter-toggle--open" : ""}`}
              onClick={() => setPopoverOpen((v) => !v)}
              aria-expanded={popoverOpen}
              aria-controls="filter-panel"
              aria-label={`Filters${activeFilterCount > 0 ? ` (${activeFilterCount} active)` : ""}`}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="4" y1="6" x2="20" y2="6" />
                <line x1="8" y1="12" x2="20" y2="12" />
                <line x1="12" y1="18" x2="20" y2="18" />
              </svg>
              {activeFilterCount > 0 && (
                <span className="sessions-filter-badge">{activeFilterCount}</span>
              )}
            </button>
          </div>
        </div>

        {/* Filter Popover */}
        {popoverOpen && (
          <FilterPopover
            anchorRef={filterBtnRef}
            onClose={() => setPopoverOpen(false)}
            project={project} setProject={handleProjectChange} projectOptions={projectOptions}
            provider={provider} setProvider={handleProviderChange} providerOptions={providerOptions}
            environment={environment} setEnvironment={handleEnvironmentChange} machineOptions={machineOptions}
            daysBack={daysBack} setDaysBack={handleDaysBackChange}
            hideAutonomous={hideAutonomous} setHideAutonomous={handleHideAutonomousChange}
            filtersLoading={filtersLoading}
          />
        )}

        {/* Recall Panel */}
        {recallOpen && (
          <div id="recall-panel" role="region" aria-label="Recall search">
            <RecallPanel project={project || undefined} />
          </div>
        )}

        {/* Timeline List */}
        {threadCards.length === 0 ? (
          <EmptyState
            title="No timeline sessions found"
            description={
              hasFilters
                ? "Try adjusting your filters or search query."
                : "Timeline entries appear once your shipper starts syncing."
            }
            action={
              hasFilters ? (
                <Button variant="secondary" onClick={handleClearFilters}>
                  Clear Filters
                </Button>
              ) : undefined
            }
          />
        ) : (
          <div className="sessions-list">
            {Array.from(groupedSessions.entries()).map(([dateKey, daySessions]) => (
              <SessionGroup
                key={dateKey}
                title={dateKey}
                sessions={daySessions}
                onSessionClick={handleSessionClick}
                onSessionPrefetch={handleSessionPrefetch}
                onSessionArchive={handleSessionArchive}
                highlightQuery={debouncedQuery}
                isSemanticResult={aiSearch}
                compatibilityMode={compatibilityMode}
                relativeNowMs={relativeNowMs}
              />
            ))}
          </div>
        )}

        {/* Footer with count and load more */}
        {total > 0 && (
          <div className="sessions-footer">
            <span className="sessions-count">
              {compatibilityMode
                ? `Showing ${threadCards.length} grouped results from ${total} matching sessions`
                : `Showing ${threadCards.length} of ${total} task threads`}
            </span>
            {hasMore && (
              <Button
                variant="secondary"
                size="sm"
                onClick={handleLoadMore}
                disabled={isLoading}
              >
                {isLoading ? "Loading..." : "Load More"}
              </Button>
            )}
          </div>
        )}
      </div>
    </PageShell>
  );
}
