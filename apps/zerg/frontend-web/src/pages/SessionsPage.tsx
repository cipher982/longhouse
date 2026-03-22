/**
 * TimelinePage - Browse agent sessions shipped via the shipper
 *
 * Features:
 * - Timeline list grouped by day
 * - Filter by project, provider, date range (dynamic from API)
 * - Search sessions by content
 * - Live updates via polling
 * - Pagination with "Load More"
 * - Click to view session details
 */

import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import { useNavigate, useSearchParams, useLocation, Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { config } from "../lib/config";
import { useAgentSessions, useAgentFilters } from "../hooks/useAgentSessions";
import { useActiveSessions, type ActiveSession } from "../hooks/useActiveSessions";
import { useClickOutside } from "../hooks/useClickOutside";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useEscapeKey } from "../hooks/useEscapeKey";
import { useTimelineSessionStream } from "../hooks/useTimelineSessionStream";
import { useReadinessFlag } from "../lib/readiness-contract";
import {
  type AgentSession,
  type AgentSessionFilters,
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
import { getProviderColor, supportsCloudContinuation } from "../lib/providers";
import { getExecutionHomeLabel } from "../lib/sessionExecutionHome";
import { resolveSessionRuntimeState } from "../lib/sessionRuntime";
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

function formatRelativeTime(dateStr: string): string {
  const date = parseUTC(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 30) return `${diffDays}d ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function getDateKey(dateStr: string): string {
  const date = parseUTC(dateStr);
  const now = new Date();
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

function getTimelineAnchor(
  session: Pick<AgentSession, "timeline_anchor_at" | "last_activity_at" | "started_at">,
): string {
  return session.timeline_anchor_at || session.last_activity_at || session.started_at;
}

function getResolvedTimelineAnchor(
  session: Pick<AgentSession, "timeline_anchor_at" | "last_activity_at" | "started_at">,
): string {
  return getTimelineAnchor(session);
}

function useDocumentVisible(): boolean {
  const [isVisible, setIsVisible] = useState(() => typeof document === "undefined" || !document.hidden);

  useEffect(() => {
    if (typeof document === "undefined") {
      return;
    }

    const handleVisibilityChange = () => {
      setIsVisible(!document.hidden);
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, []);

  return isVisible;
}

interface SessionThreadCard {
  threadId: string;
  head: AgentSession;
  detail: AgentSession;
  root: AgentSession;
  sessions: AgentSession[];
  continuationCount: number;
  startedOriginLabel: string | null;
  headOriginLabel: string | null;
}

function buildThreadCards(
  sessions: AgentSession[],
): SessionThreadCard[] {
  const groups = new Map<string, {
    order: number;
    sessions: AgentSession[];
    detail: AgentSession;
    head: AgentSession | null;
  }>();

  sessions.forEach((session, index) => {
    const threadId = session.thread_root_session_id || session.id;
    const existing = groups.get(threadId);
    if (!existing) {
      groups.set(threadId, {
        order: index,
        sessions: [session],
        detail: session,
        head: session.id === session.thread_head_session_id || session.is_writable_head ? session : null,
      });
      return;
    }

    existing.sessions.push(session);
    if (session.id === session.thread_head_session_id || session.is_writable_head) {
      existing.head = session;
    }
  });

  return Array.from(groups.entries())
    .map(([threadId, group]) => {
      const orderedSessions = [...group.sessions].sort(
        (a, b) => new Date(a.started_at).getTime() - new Date(b.started_at).getTime(),
      );
      const root = orderedSessions.find((session) => session.id === threadId) || orderedSessions[0] || group.detail;
      const head =
        group.head ||
        orderedSessions.find((session) => session.id === group.detail.thread_head_session_id) ||
        group.detail;

      return {
        order: group.order,
        threadId,
        head,
        detail: group.detail,
        root,
        sessions: orderedSessions,
        continuationCount: head.thread_continuation_count || orderedSessions.length,
        startedOriginLabel: root.origin_label || root.environment,
        headOriginLabel: head.origin_label || head.environment,
      };
    })
    .sort((a, b) => {
      const anchorDiff =
        parseUTC(getResolvedTimelineAnchor(b.head)).getTime() -
        parseUTC(getResolvedTimelineAnchor(a.head)).getTime();
      if (anchorDiff !== 0) return anchorDiff;
      return a.order - b.order;
    })
    .map(({ order: _order, ...card }) => card);
}

function groupThreadCardsByDay(
  cards: SessionThreadCard[],
): Map<string, SessionThreadCard[]> {
  const groups = new Map<string, SessionThreadCard[]>();

  for (const card of cards) {
    const key = getDateKey(getResolvedTimelineAnchor(card.head));
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


function sessionSortKey(status: string): number {
  if (status === "working") return 0;
  if (status === "idle") return 1;
  return 2;
}

function isSessionLive(session: ActiveSession): boolean {
  return resolveSessionRuntimeState(session, session).isLive;
}

function getRuntimeMetaLabel(runtime: ReturnType<typeof resolveSessionRuntimeState>): string | null {
  if (runtime.truthTier === "managed-local") {
    return "Local runtime";
  }
  if (runtime.truthTier === "fresh") {
    return "Fresh signal";
  }
  if (runtime.truthTier === "inferred") {
    return "Recent progress";
  }
  if (runtime.lastLiveAt) {
    return `Seen ${formatRelativeTime(runtime.lastLiveAt)}`;
  }
  if (runtime.truthTier === "stale" || runtime.confidence === "stale") {
    return "Stale";
  }
  return null;
}

function repoNameFromUrl(url: string | null): string | null {
  if (!url) return null;
  const cleaned = url.replace(/\.git$/, "");
  const parts = cleaned.split("/");
  return parts[parts.length - 1] || null;
}

function cwdBasename(cwd: string | null): string | null {
  if (!cwd) return null;
  const parts = cwd.split("/").filter(Boolean);
  return parts[parts.length - 1] || null;
}

function truncateText(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 3).trim()}...`;
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

function getLiveSessionScope(session: ActiveSession): string {
  return (
    session.project?.trim() ||
    repoNameFromUrl(session.git_repo) ||
    cwdBasename(session.cwd) ||
    "workspace"
  );
}

function getLiveSessionTitle(session: ActiveSession): string {
  const candidate = (session.last_user_message || session.last_assistant_message || "")
    .trim()
    .replace(/\s+/g, " ");
  if (candidate) {
    return truncateText(candidate, 56);
  }
  return getLiveSessionScope(session);
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
  thread: SessionThreadCard;
  onClick: () => void;
  highlightQuery?: string;
  isSemanticResult?: boolean;
}

function SessionCard({ thread, onClick, highlightQuery, isSemanticResult }: SessionCardProps) {
  const session = thread.head;
  const detailSession = thread.detail;
  const turnCount = session.user_messages;
  const toolCount = session.tool_calls;
  const runtime = resolveSessionRuntimeState(session);
  const runtimeMetaLabel = getRuntimeMetaLabel(runtime);

  const projectLabel = getProjectLabel(session);
  const title = getSessionTitle(session);
  const executionHomeLabel = getExecutionHomeLabel(session.execution_home);
  const showHeadOriginLabel = !!thread.headOriginLabel && thread.headOriginLabel !== executionHomeLabel;

  const showKeywordSnippet = !isSemanticResult && !!highlightQuery && !!detailSession.match_snippet;
  const showSemanticSnippet = isSemanticResult && !!detailSession.match_snippet;
  const showSummary = !showKeywordSnippet && !showSemanticSnippet && !!session.summary;
  const showGenerating = !showKeywordSnippet && !showSemanticSnippet && !session.summary && !session.summary_title;
  const primaryActionLabel = supportsCloudContinuation(session.provider) ? "Continue in cloud" : "Latest context";
  const cardClassName = [
    "session-card",
    runtime.isLive ? "session-card--live" : "",
    runtime.isIdle ? "session-card--idle" : "",
    runtime.heuristicActive ? "session-card--inferred" : "",
    runtime.tone === "running" ? "session-card--running" : "",
    runtime.tone === "needs-user" ? "session-card--needs-user" : "",
    runtime.tone === "blocked" ? "session-card--blocked" : "",
  ].filter(Boolean).join(" ");

  return (
    <Card
      className={cardClassName}
      onClick={onClick}
      style={{ borderLeftColor: getProviderColor(session.provider) }}
    >
      <div className="session-card-header">
        <div className="session-card-project">{projectLabel}</div>
        <span className="session-card-time">{formatRelativeTime(getResolvedTimelineAnchor(session))}</span>
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
          <span className="environment-badge environment-badge--secondary">Head: {thread.headOriginLabel}</span>
        )}
        {thread.continuationCount > 1 && thread.startedOriginLabel && thread.startedOriginLabel !== thread.headOriginLabel && (
          <span className="environment-badge environment-badge--secondary">Started: {thread.startedOriginLabel}</span>
        )}
        {thread.continuationCount > 1 && (
          <span className="environment-badge environment-badge--secondary">
            {thread.continuationCount} continuations
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

      <div className="session-card-footer">
        <div className="session-card-stats">
          <span className="session-stat" style={{ color: getTurnsColor(turnCount) }}>{turnCount} {turnCount === 1 ? 'turn' : 'turns'}</span>
          <span className="session-stat-separator">&middot;</span>
          <span className="session-stat">{toolCount} {toolCount === 1 ? 'tool' : 'tools'}</span>
          <span className="session-stat-separator">&middot;</span>
          <span className="session-stat session-stat--secondary">Started {formatRelativeTime(thread.root.started_at)}</span>
        </div>
        <div className="session-card-actions">
          <span className="session-card-action-label">{primaryActionLabel}</span>
          <span className="session-card-arrow">&rarr;</span>
        </div>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Session Group Component
// ---------------------------------------------------------------------------

interface SessionGroupProps {
  title: string;
  sessions: SessionThreadCard[];
  onSessionClick: (thread: SessionThreadCard) => void;
  highlightQuery?: string;
  isSemanticResult?: boolean;
}

function SessionGroup({
  title,
  sessions,
  onSessionClick,
  highlightQuery,
  isSemanticResult,
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
            key={thread.threadId}
            thread={thread}
            onClick={() => onSessionClick(thread)}
            highlightQuery={highlightQuery}
            isSemanticResult={isSemanticResult}
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
  const [liveViewOpen, setLiveViewOpen] = useState(false);

  // Fetch dynamic filter options
  const { data: filtersData, isLoading: filtersLoading } = useAgentFilters(daysBack);
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
  const timelineStreamEnabled = timelineStreamEligible && documentVisible;

  // Single unified query — use SSE for live rows and keep a slow polling backstop.
  const activeResult = useAgentSessions(filters, {
    refetchInterval: timelineStreamEligible
      ? TIMELINE_RECONCILIATION_MS
      : (query) => {
          const pendingSessions = query.state.data?.sessions?.some((session) => !session.summary_title && !session.summary);
          return pendingSessions ? 3_000 : 30_000;
        },
  });
  const data = activeResult.data;
  const isLoading = activeResult.isLoading;
  const error = activeResult.error;
  const refetch = activeResult.refetch;

  const sessions = useMemo(() => data?.sessions || [], [data?.sessions]);
  const total = data?.total || 0;
  const hasRealSessions = data?.has_real_sessions ?? true;
  const hasMore = sessions.length < total;

  useTimelineSessionStream(filters, { enabled: timelineStreamEnabled });

  const {
    data: activeSessionsData,
    isLoading: activeSessionsLoading,
    error: activeSessionsError,
  } = useActiveSessions({
    pollInterval: 2000,
    limit: Math.max(limit, PAGE_SIZE),
    days_back: daysBack,
    project: project || undefined,
    enabled: liveViewOpen,
  });

  const activeSessions = useMemo(() => {
    const list = activeSessionsData?.sessions ?? [];
    return [...list].sort((a, b) => {
      const groupDiff = sessionSortKey(a.status) - sessionSortKey(b.status);
      if (groupDiff !== 0) return groupDiff;
      return parseUTC(b.timeline_anchor_at || b.last_activity_at).getTime() - parseUTC(a.timeline_anchor_at || a.last_activity_at).getTime();
    });
  }, [activeSessionsData]);

  const liveOverlaySessions = liveViewOpen ? activeSessions : [];
  const liveTotal = liveOverlaySessions.length;
  const liveCount = useMemo(
    () => liveOverlaySessions.filter(isSessionLive).length,
    [liveOverlaySessions]
  );

  const liveAuthError = (activeSessionsError as { status?: number } | null)?.status === 401;
  const liveList = useMemo(() => liveOverlaySessions.slice(0, 8), [liveOverlaySessions]);
  const threadCards = useMemo(() => buildThreadCards(sessions), [sessions]);
  const groupedSessions = useMemo(() => groupThreadCardsByDay(threadCards), [threadCards]);

  const headerActions = (
    <div className="sessions-header-actions">
      {threadCards.length > 0 && <span className="sessions-header-count">{threadCards.length} tasks</span>}
      <Button
        variant="ghost"
        size="sm"
        onClick={() => navigate("/briefings")}
      >
        Briefings
      </Button>
      <Button
        variant={liveViewOpen ? "primary" : "secondary"}
        size="sm"
        onClick={() => setLiveViewOpen((prev) => !prev)}
        aria-expanded={liveViewOpen}
        aria-controls="sessions-live-view"
      >
        {liveViewOpen ? "Hide live view" : "Live view"}
      </Button>
    </div>
  );

  // Handle session click - preserve current filters in location state
  const handleSessionClick = useCallback((thread: SessionThreadCard) => {
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
  const queryClient = useQueryClient();
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

        {liveViewOpen && (
          <div id="sessions-live-view">
            <Card className="sessions-live-panel">
            <div className="sessions-live-header">
              <div>
                <div className="sessions-live-title">Live View</div>
                <div className="sessions-live-subtitle">
                  {activeSessionsLoading && liveTotal === 0
                    ? "Checking live sessions..."
                    : liveTotal === 0
                      ? `No live sessions in the last ${daysBack} days`
                      : `${liveCount} active · ${liveTotal} total (last ${daysBack} days)`
                  }
                </div>
              </div>
            </div>
            <div className="sessions-live-body">
              {liveAuthError ? (
                <div className="sessions-live-empty">
                  <span>Session expired.</span>
                  <Button variant="primary" size="sm" onClick={() => window.location.reload()}>
                    Refresh to log in
                  </Button>
                </div>
              ) : activeSessionsLoading && liveTotal === 0 ? (
                <div className="sessions-live-empty">
                  <Spinner size="lg" />
                  <span>Loading live view...</span>
                </div>
              ) : liveList.length === 0 ? (
                <div className="sessions-live-empty">
                  <span>No recent sessions.</span>
                  <span className="sessions-live-empty-subtitle">Start a CLI session to populate this list.</span>
                </div>
              ) : (
                <div className="sessions-live-list">
                  {liveList.map((session) => {
                    const runtime = resolveSessionRuntimeState(session, session);
                    const isActive = runtime.isLive;
                    const rowClass = [
                      "sessions-live-row",
                      isActive ? "sessions-live-row--active" : "",
                    ].filter(Boolean).join(" ");

                    return (
                      <button
                        key={session.id}
                        type="button"
                        className={rowClass}
                        onClick={() => {
                          navigate(buildSessionDetailPath(session));
                        }}
                      >
                        <div className="sessions-live-row-title">
                          {getLiveSessionTitle(session)}
                        </div>
                        <div className="sessions-live-row-meta">
                          {getLiveSessionScope(session)} · {session.provider} ·{" "}
                          {formatRelativeTime(session.timeline_anchor_at || session.last_activity_at)}
                        </div>
                        <div className="sessions-live-row-presence">
                          <PresenceBadge
                            state={runtime.presenceState}
                            tool={runtime.presenceTool}
                            compact
                            heuristicActive={runtime.heuristicActive}
                            showUnknown={runtime.truthTier === "stale"}
                          />
                          <span className="sessions-live-row-presence-label">
                            {runtime.displayPhase}
                          </span>
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
            </Card>
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
                highlightQuery={debouncedQuery}
                isSemanticResult={aiSearch}
              />
            ))}
          </div>
        )}

        {/* Footer with count and load more */}
        {total > 0 && (
          <div className="sessions-footer">
            <span className="sessions-count">
              {threadCards.length === sessions.length
                ? `Showing ${threadCards.length} of ${total} sessions`
                : `Showing ${threadCards.length} task threads from ${sessions.length} sessions`}
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
