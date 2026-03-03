/**
 * SessionDetailPage - View detailed event timeline for an agent session
 *
 * Features:
 * - Full event timeline (user, assistant, tool)
 * - Tool calls and their results merged into single collapsible cards
 * - Session metadata header
 * - Back navigation
 */

import { useState, useEffect, useMemo } from "react";
import { useParams, useNavigate, useLocation, useSearchParams } from "react-router-dom";
import { useAgentSession, useAgentSessionEventsInfinite } from "../hooks/useAgentSessions";
import type { AgentEvent } from "../services/api/agents";
import {
  Button,
  Badge,
  SectionHeader,
  EmptyState,
  PageShell,
  Spinner,
} from "../components/ui";
import { SessionChat } from "../components/SessionChat";
import type { ActiveSession } from "../hooks/useActiveSessions";
import { parseUTC } from "../lib/dateUtils";
import "../styles/sessions.css";

const EVENTS_PAGE_SIZE = 1000;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatTime(dateStr: string): string {
  return parseUTC(dateStr).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatFullDate(dateStr: string): string {
  return parseUTC(dateStr).toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDuration(startedAt: string, endedAt: string | null): string {
  if (!endedAt) return "In progress";
  const start = parseUTC(startedAt);
  const end = parseUTC(endedAt);
  const diffMs = end.getTime() - start.getTime();
  const diffMins = Math.floor(diffMs / 60000);

  if (diffMins < 1) return "<1m";
  if (diffMins < 60) return `${diffMins} min`;
  const hours = Math.floor(diffMins / 60);
  const mins = diffMins % 60;
  return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
}

function getProviderColor(provider: string): string {
  switch (provider) {
    case "claude":
      return "var(--color-brand-accent)";
    case "codex":
      return "var(--color-intent-success)";
    case "gemini":
      return "var(--color-neon-cyan)";
    default:
      return "var(--color-text-secondary)";
  }
}

function truncatePath(path: string | null, maxLen: number = 50): string {
  if (!path) return "";
  if (path.length <= maxLen) return path;
  const parts = path.split("/");
  if (parts.length <= 3) return "..." + path.slice(-maxLen);
  return "~/" + parts.slice(-3).join("/");
}

function getToolDisplayInfo(toolName: string): { icon: string; color: string } {
  switch (toolName.toLowerCase()) {
    case "bash":
    case "exec_command":
    case "shell":
    case "shell_command":
    case "run_shell_command":
    case "write_stdin":
      return { icon: "$", color: "var(--color-intent-warning)" };
    case "read":
    case "read_file":
      return { icon: "R", color: "var(--color-neon-cyan)" };
    case "write":
    case "create_file":
      return { icon: "W", color: "var(--color-intent-success)" };
    case "edit":
    case "str_replace_editor":
      return { icon: "E", color: "var(--color-brand-primary)" };
    case "grep":
      return { icon: "~", color: "var(--color-text-secondary)" };
    case "glob":
      return { icon: "*", color: "var(--color-text-secondary)" };
    case "task":
      return { icon: "T", color: "var(--color-neon-secondary)" };
    case "todowrite":
    case "update_plan":
      return { icon: "✓", color: "var(--color-brand-accent)" };
    default:
      return { icon: (toolName[0] || "·").toUpperCase(), color: "var(--color-text-secondary)" };
  }
}

// ---------------------------------------------------------------------------
// View-model types
// ---------------------------------------------------------------------------

/** A single merged tool interaction (call + its result, if any). */
type ToolInteraction = {
  /** Stable key for expand/collapse state. */
  key: string;
  toolName: string;
  callEvent: AgentEvent | null;
  resultEvent: AgentEvent | null;
  /** How call and result were paired. */
  pairing: "id" | "fifo" | "orphan" | "pending";
  /** DOM id anchor — always the call event id when present, else result id. */
  anchorId: number;
  timestamp: string;
};

type TimelineItem =
  | { kind: "message"; event: AgentEvent }
  | { kind: "tool"; interaction: ToolInteraction };

/**
 * Build the merged timeline view-model from raw events.
 *
 * Two passes:
 * 1. Pair every tool-call event with its result using tool_call_id (precise)
 *    or FIFO position (legacy rows without IDs).
 * 2. Emit TimelineItems in order, skipping result events that were absorbed
 *    into their call's ToolInteraction.
 */
function buildTimelineItems(events: AgentEvent[]): {
  items: TimelineItem[];
  toolItems: ToolInteraction[];
  /** Maps any event id (call or result) to the anchor id of its merged card. */
  eventIdToAnchor: Map<number, number>;
} {
  // Pass 1 ── build ToolInteractions
  const byCallId = new Map<string, ToolInteraction>();   // tool_call_id → interaction
  const byCallEventId = new Map<number, ToolInteraction>(); // call event id → interaction
  const fifoQueue: ToolInteraction[] = [];               // unmatched legacy calls
  const absorbedResultIds = new Set<number>();
  const eventIdToAnchor = new Map<number, number>();

  for (const e of events) {
    if (e.role === "assistant" && e.tool_name) {
      const key = e.tool_call_id ? `id:${e.tool_call_id}` : `call:${e.id}`;
      const interaction: ToolInteraction = {
        key,
        toolName: e.tool_name,
        callEvent: e,
        resultEvent: null,
        pairing: e.tool_call_id ? "id" : "pending",
        anchorId: e.id,
        timestamp: e.timestamp,
      };
      byCallEventId.set(e.id, interaction);
      if (e.tool_call_id) {
        byCallId.set(e.tool_call_id, interaction);
      } else {
        fifoQueue.push(interaction);
      }
      eventIdToAnchor.set(e.id, e.id);

    } else if (e.role === "tool") {
      let matched: ToolInteraction | undefined;

      if (e.tool_call_id) {
        matched = byCallId.get(e.tool_call_id);
        // ID present but no matching call (mid-rollout / orphan) → fall through to FIFO
      }
      if (!matched) {
        matched = fifoQueue.shift();
        if (matched) matched.pairing = "fifo";
      }

      if (matched) {
        matched.resultEvent = e;
        absorbedResultIds.add(e.id);
        eventIdToAnchor.set(e.id, matched.anchorId);
      } else {
        // Genuine orphan result — no call found
        eventIdToAnchor.set(e.id, e.id);
      }
    }
  }

  // Pass 2 ── build TimelineItems in order
  const items: TimelineItem[] = [];
  const toolItems: ToolInteraction[] = [];

  for (const e of events) {
    // Skip result events absorbed into their call's card
    if (e.role === "tool" && absorbedResultIds.has(e.id)) continue;

    if (e.role === "user") {
      items.push({ kind: "message", event: e });

    } else if (e.role === "assistant" && e.tool_name) {
      const interaction = byCallEventId.get(e.id)!;
      items.push({ kind: "tool", interaction });
      toolItems.push(interaction);

    } else if (e.role === "tool") {
      // Orphan result (call was never recorded)
      const interaction: ToolInteraction = {
        key: `orphan:${e.id}`,
        toolName: "tool",
        callEvent: null,
        resultEvent: e,
        pairing: "orphan",
        anchorId: e.id,
        timestamp: e.timestamp,
      };
      items.push({ kind: "tool", interaction });
      toolItems.push(interaction);

    } else {
      // assistant text (no tool_name) or unknown role
      items.push({ kind: "message", event: e });
    }
  }

  return { items, toolItems, eventIdToAnchor };
}

// ---------------------------------------------------------------------------
// Event Components
// ---------------------------------------------------------------------------

interface UserMessageProps {
  event: AgentEvent;
  isHighlighted?: boolean;
}

function UserMessage({ event, isHighlighted }: UserMessageProps) {
  return (
    <div
      id={`event-${event.id}`}
      className={`event-item event-user${isHighlighted ? " event-highlight" : ""}`}
    >
      <div className="event-header">
        <span className="event-role event-role-user">You</span>
        <span className="event-time">{formatTime(event.timestamp)}</span>
      </div>
      <div className="event-content user-content">
        {event.content_text || "(empty message)"}
      </div>
    </div>
  );
}

interface AssistantMessageProps {
  event: AgentEvent;
  isHighlighted?: boolean;
}

function AssistantMessage({ event, isHighlighted }: AssistantMessageProps) {
  return (
    <div
      id={`event-${event.id}`}
      className={`event-item event-assistant${isHighlighted ? " event-highlight" : ""}`}
    >
      <div className="event-header">
        <span className="event-role event-role-assistant">AI</span>
        <span className="event-time">{formatTime(event.timestamp)}</span>
      </div>
      <div className="event-content assistant-content">
        {event.content_text || "(thinking...)"}
      </div>
    </div>
  );
}

interface ToolInteractionCardProps {
  interaction: ToolInteraction;
  isExpanded: boolean;
  onToggle: () => void;
  isHighlighted?: boolean;
}

function ToolInteractionCard({
  interaction,
  isExpanded,
  onToggle,
  isHighlighted,
}: ToolInteractionCardProps) {
  const { toolName, callEvent, resultEvent, pairing } = interaction;
  const toolInfo = getToolDisplayInfo(toolName);

  const hasInput =
    callEvent?.tool_input_json != null &&
    Object.keys(callEvent.tool_input_json).length > 0;
  const hasOutput =
    resultEvent?.tool_output_text != null &&
    resultEvent.tool_output_text.length > 0;

  const isPending = !resultEvent && pairing !== "orphan";
  const isOrphan = pairing === "orphan";

  const getSummary = (): string => {
    if (callEvent?.tool_input_json) {
      const input = callEvent.tool_input_json;
      if ("file_path" in input) return truncatePath(String(input.file_path));
      if ("command" in input) return String(input.command).slice(0, 60);
      if ("pattern" in input) return String(input.pattern);
      if ("path" in input) return truncatePath(String(input.path));
      if ("url" in input) return String(input.url).slice(0, 50);
    }
    if (resultEvent?.tool_output_text) {
      return resultEvent.tool_output_text.slice(0, 80).replace(/\n/g, " ");
    }
    return "";
  };

  const summary = getSummary();
  const timestamp = callEvent?.timestamp ?? resultEvent?.timestamp ?? "";

  return (
    <div
      id={`event-${interaction.anchorId}`}
      className={`event-item event-tool ${isExpanded ? "expanded" : ""}${isHighlighted ? " event-highlight" : ""}`}
    >
      <button
        className="event-tool-header"
        onClick={onToggle}
        aria-expanded={isExpanded}
      >
        <div className="event-tool-title">
          <span
            className="tool-icon"
            style={{ backgroundColor: toolInfo.color, opacity: isOrphan ? 0.5 : 1 }}
          >
            {toolInfo.icon}
          </span>
          <span className="tool-name">{toolName}</span>
          {isPending && <span className="tool-pending-badge">…</span>}
          {!isExpanded && summary && (
            <span className="tool-summary">{summary}</span>
          )}
        </div>
        <div className="event-tool-meta">
          {timestamp && <span className="event-time">{formatTime(timestamp)}</span>}
          <span className="expand-icon">{isExpanded ? "▼" : "▶"}</span>
        </div>
      </button>

      {isExpanded && (
        <div className="event-tool-body">
          {hasInput && (
            <div className="tool-section">
              <div className="tool-section-label">Input</div>
              <pre className="tool-section-content">
                {JSON.stringify(callEvent!.tool_input_json, null, 2)}
              </pre>
            </div>
          )}
          {hasOutput && (
            <div className="tool-section">
              <div className="tool-section-label">Output</div>
              <pre className="tool-section-content tool-output">
                {resultEvent!.tool_output_text}
              </pre>
            </div>
          )}
          {isPending && (
            <div className="tool-section-empty">
              Result not recorded — session ended mid-execution
            </div>
          )}
          {isOrphan && !hasOutput && (
            <div className="tool-section-empty">No output recorded</div>
          )}
          {!hasInput && !hasOutput && !isPending && !isOrphan && (
            <div className="tool-section-empty">No input/output recorded</div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export default function SessionDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();

  const highlightEventId = useMemo(() => {
    const raw = searchParams.get("event_id");
    if (!raw) return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  }, [searchParams]);

  const shouldAutoResume = useMemo(() => searchParams.get("resume") === "1", [searchParams]);

  // Fetch session and events
  const { data: session, isLoading: sessionLoading, error: sessionError } =
    useAgentSession(sessionId || null);
  const {
    data: eventsPagesData,
    isLoading: eventsLoading,
    error: eventsError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useAgentSessionEventsInfinite(sessionId || null, { limit: EVENTS_PAGE_SIZE });

  const events = useMemo(
    () => eventsPagesData?.pages.flatMap((page) => page.events) || [],
    [eventsPagesData]
  );
  const totalEvents = useMemo(
    () => eventsPagesData?.pages[0]?.total ?? events.length,
    [eventsPagesData, events.length]
  );

  // Build merged timeline view-model
  const { items: timelineItems, toolItems, eventIdToAnchor } = useMemo(
    () => buildTimelineItems(events),
    [events]
  );

  // Resume chat state
  const [showResume, setShowResume] = useState(false);

  // Legacy forum redirects can pass ?resume=1 to drop directly into chat mode.
  // Consume it once and strip it from URL so close/back behavior stays predictable.
  useEffect(() => {
    if (!shouldAutoResume || !session) return;

    if (session.provider === "claude") {
      setShowResume(true);
    }

    const next = new URLSearchParams(searchParams);
    next.delete("resume");
    navigate(
      {
        pathname: location.pathname,
        search: next.toString() ? `?${next.toString()}` : "",
      },
      { replace: true }
    );
  }, [shouldAutoResume, session, searchParams, navigate, location.pathname]);

  // Event role filter
  const [eventFilter, setEventFilter] = useState<"all" | "messages" | "tools">("all");

  // Text search with debounce
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(searchQuery), 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  // Expand/collapse state keyed by interaction key (string)
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  const [highlightedAnchorId, setHighlightedAnchorId] = useState<string | null>(null);

  const toggleTool = (key: string) => {
    setExpandedTools((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const allExpanded =
    toolItems.length > 0 && toolItems.every((i) => expandedTools.has(i.key));

  const toggleAll = () => {
    if (allExpanded) {
      setExpandedTools(new Set());
    } else {
      setExpandedTools(new Set(toolItems.map((i) => i.key)));
    }
  };

  // Filter + search over timeline items
  const filteredItems = useMemo(() => {
    let result = timelineItems;

    if (eventFilter === "messages") {
      result = result.filter((item) => item.kind === "message");
    } else if (eventFilter === "tools") {
      result = result.filter((item) => item.kind === "tool");
    }

    if (debouncedSearch.trim()) {
      const q = debouncedSearch.toLowerCase();
      result = result.filter((item) => {
        if (item.kind === "message") {
          return item.event.content_text?.toLowerCase().includes(q);
        }
        // tool: search name, input, output
        const { interaction } = item;
        if (interaction.toolName.toLowerCase().includes(q)) return true;
        if (interaction.callEvent?.tool_input_json &&
          JSON.stringify(interaction.callEvent.tool_input_json).toLowerCase().includes(q))
          return true;
        if (interaction.resultEvent?.tool_output_text?.toLowerCase().includes(q))
          return true;
        return false;
      });
    }

    return result;
  }, [timelineItems, eventFilter, debouncedSearch]);

  const messageCount = useMemo(
    () => timelineItems.filter((i) => i.kind === "message").length,
    [timelineItems]
  );
  const hasHighlightEvent = useMemo(() => {
    if (highlightEventId == null) {
      return true;
    }
    return events.some((event) => event.id === highlightEventId);
  }, [highlightEventId, events]);

  // For deep-link anchors, auto-fetch additional pages until the target event appears
  // or we exhaust pagination.
  useEffect(() => {
    if (highlightEventId == null) return;
    if (hasHighlightEvent) return;
    if (!hasNextPage || isFetchingNextPage) return;
    void fetchNextPage();
  }, [highlightEventId, hasHighlightEvent, hasNextPage, isFetchingNextPage, fetchNextPage]);

  // Ready signal for E2E
  useEffect(() => {
    if (!sessionLoading && !eventsLoading) {
      document.body.setAttribute("data-ready", "true");
      document.body.setAttribute("data-screenshot-ready", "true");
    }
    return () => {
      document.body.removeAttribute("data-ready");
      document.body.removeAttribute("data-screenshot-ready");
    };
  }, [sessionLoading, eventsLoading]);

  // Scroll to event when arriving from search results.
  // Map raw event_id → anchor id of its merged card.
  useEffect(() => {
    if (!highlightEventId || events.length === 0) return;
    const anchorId = eventIdToAnchor.get(highlightEventId) ?? highlightEventId;
    const domId = `event-${anchorId}`;
    const target = document.getElementById(domId);
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      setHighlightedAnchorId(domId);
      // Auto-expand the highlighted tool card
      const toolItem = toolItems.find((i) => i.anchorId === anchorId);
      if (toolItem) {
        setExpandedTools((prev) => new Set([...prev, toolItem.key]));
      }
    }
  }, [highlightEventId, events, eventIdToAnchor, toolItems]);

  // Back navigation
  const handleBack = () => {
    const from = (location.state as { from?: string })?.from;
    navigate(from ?? "/timeline");
  };

  if (sessionLoading || eventsLoading) {
    return (
      <PageShell size="wide" className="sessions-page-container">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading session..."
          description="Fetching session details."
        />
      </PageShell>
    );
  }

  const error = sessionError || eventsError;
  if (error || !session) {
    return (
      <PageShell size="wide" className="sessions-page-container">
        <EmptyState
          variant="error"
          title="Error loading session"
          description={
            error instanceof Error
              ? error.message
              : "Session not found or failed to load."
          }
          action={
            <Button variant="primary" onClick={handleBack}>
              Back to Timeline
            </Button>
          }
        />
      </PageShell>
    );
  }

  const title =
    session.summary_title && session.summary_title !== "Untitled Session"
      ? session.summary_title
      : session.project || session.git_branch || "Session";

  const canResume = session.provider === "claude";

  const activeSessionForChat: ActiveSession | null = canResume
    ? {
        id: session.id,
        project: session.project,
        provider: session.provider,
        cwd: session.cwd,
        git_repo: session.git_repo,
        git_branch: session.git_branch,
        started_at: session.started_at,
        ended_at: session.ended_at,
        last_activity_at: session.ended_at || session.started_at,
        status: session.ended_at ? "completed" : "working",
        attention: "auto",
        duration_minutes: 0,
        last_user_message: null,
        last_assistant_message: null,
        message_count: session.user_messages + session.assistant_messages,
        tool_calls: session.tool_calls,
        presence_state: null,
        presence_tool: null,
        presence_updated_at: null,
        user_state: "active",
      }
    : null;

  if (showResume && activeSessionForChat) {
    return (
      <PageShell size="wide" className="sessions-page-container">
        <div className="session-detail-page">
          <div className="session-resume-container">
            <SessionChat
              session={activeSessionForChat}
              onClose={() => setShowResume(false)}
            />
          </div>
        </div>
      </PageShell>
    );
  }

  return (
    <PageShell size="wide" className="sessions-page-container">
      <div className="session-detail-page">
        {/* Header */}
        <div className="session-detail-header">
          <div className="session-detail-nav">
            <span className="session-breadcrumb">
              <Button variant="ghost" onClick={handleBack} className="back-button">
                &larr; Timeline
              </Button>
              <span className="breadcrumb-separator">/</span>
              <span className="breadcrumb-current">{title}</span>
            </span>
            <div className="session-detail-actions">
              {canResume && (
                <Button variant="primary" size="sm" onClick={() => setShowResume(true)}>
                  Resume Session
                </Button>
              )}
              {toolItems.length > 0 && (
                <Button variant="ghost" size="sm" onClick={toggleAll}>
                  {allExpanded ? "Collapse All" : "Expand All"}
                </Button>
              )}
            </div>
          </div>
        </div>

        {/* Compact metadata row */}
        <div className="session-detail-meta">
          <div className="meta-item">
            <span
              className="provider-dot"
              style={{ backgroundColor: getProviderColor(session.provider) }}
            />
            <span className="meta-value">{session.provider}</span>
          </div>
          <span className="meta-separator">&middot;</span>
          <div className="meta-item">
            <span className={`session-status-badge ${session.ended_at ? "completed" : "in-progress"}`}>
              <span className={`status-dot ${session.ended_at ? "completed" : "in-progress"}`} />
              {session.ended_at ? "Completed" : "In Progress"}
            </span>
          </div>
          <span className="meta-separator">&middot;</span>
          <div className="meta-item">
            <span className="meta-value">{formatFullDate(session.started_at)}</span>
          </div>
          <span className="meta-separator">&middot;</span>
          <div className="meta-item">
            <span className="meta-value">
              {formatDuration(session.started_at, session.ended_at)}
            </span>
          </div>
          <span className="meta-separator">&middot;</span>
          <div className="meta-item">
            <Badge variant="neutral">{session.user_messages} turns</Badge>
            <Badge variant="neutral">{session.tool_calls} tools</Badge>
          </div>
          {session.environment && session.environment !== "production" && (
            <>
              <span className="meta-separator">&middot;</span>
              <div className="meta-item">
                <span className={`environment-badge environment-badge--${session.environment}`}>
                  {session.environment}
                </span>
              </div>
            </>
          )}
          {session.git_branch && (
            <>
              <span className="meta-separator">&middot;</span>
              <div className="meta-item">
                <span className="git-branch">
                  <span className="branch-icon">&#x2387;</span>
                  {session.git_branch}
                </span>
              </div>
            </>
          )}
          {session.cwd && (
            <>
              <span className="meta-separator">&middot;</span>
              <div className="meta-item">
                <span className="meta-value text-muted">{truncatePath(session.cwd, 50)}</span>
              </div>
            </>
          )}
        </div>

        {/* Collapsible summary */}
        {session.summary && (
          <details className="session-detail-summary">
            <summary className="session-detail-summary-label">Summary</summary>
            <div className="session-detail-summary-text">{session.summary}</div>
          </details>
        )}

        {/* Event Timeline */}
        <div className="session-timeline">
          <div className="session-timeline-controls">
            <div className="timeline-header">
              <span className="timeline-title">Event Timeline</span>
              <span className="timeline-count">
                {events.length >= totalEvents
                  ? `${totalEvents} events`
                  : `${events.length}/${totalEvents} events loaded`}
              </span>
            </div>
            {events.length > 20 && (
              <div className="timeline-scroll-hint" role="note">
                Tip: scroll anywhere in the viewport.
              </div>
            )}
            {highlightEventId != null && !hasHighlightEvent && (
              <div className="timeline-scroll-hint" role="status">
                Loading more events to locate anchor `{highlightEventId}`…
              </div>
            )}

            {timelineItems.length > 0 && (
              <div className="session-detail-filters">
                <div className="filter-btn-group">
                  {(["all", "messages", "tools"] as const).map((filter) => (
                    <button
                      key={filter}
                      className={`filter-btn ${eventFilter === filter ? "active" : ""}`}
                      onClick={() => setEventFilter(filter)}
                    >
                      {filter === "all"
                        ? `All (${timelineItems.length})`
                        : filter === "messages"
                        ? `Messages (${messageCount})`
                        : `Tools (${toolItems.length})`}
                    </button>
                  ))}
                </div>
                <div className="event-search-wrapper">
                  <input
                    type="text"
                    className="event-search-input"
                    placeholder="Search events..."
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                  />
                  {debouncedSearch.trim() && (
                    <span className="event-search-count">
                      {filteredItems.length} match
                      {filteredItems.length !== 1 ? "es" : ""}
                    </span>
                  )}
                </div>
              </div>
            )}
            {hasNextPage && (
              <div className="session-detail-pagination">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => void fetchNextPage()}
                  disabled={isFetchingNextPage}
                >
                  {isFetchingNextPage ? "Loading more…" : "Load older events"}
                </Button>
              </div>
            )}
          </div>

          {filteredItems.length === 0 ? (
            <EmptyState
              title="No events"
              description={
                debouncedSearch.trim()
                  ? `No events match "${debouncedSearch}".`
                  : eventFilter !== "all"
                  ? "No events match the selected filter."
                  : "This session has no recorded events."
              }
            />
          ) : (
            <div className="timeline-events">
              {filteredItems.map((item) => {
                if (item.kind === "tool") {
                  const { interaction } = item;
                  const isHighlighted =
                    highlightedAnchorId === `event-${interaction.anchorId}`;
                  return (
                    <ToolInteractionCard
                      key={interaction.key}
                      interaction={interaction}
                      isExpanded={expandedTools.has(interaction.key)}
                      onToggle={() => toggleTool(interaction.key)}
                      isHighlighted={isHighlighted}
                    />
                  );
                }

                // kind === "message"
                const { event } = item;
                const isHighlighted = highlightedAnchorId === `event-${event.id}`;
                if (event.role === "user") {
                  return (
                    <UserMessage key={event.id} event={event} isHighlighted={isHighlighted} />
                  );
                }
                if (event.role === "assistant") {
                  return (
                    <AssistantMessage key={event.id} event={event} isHighlighted={isHighlighted} />
                  );
                }
                // Unknown role
                return (
                  <div
                    key={event.id}
                    id={`event-${event.id}`}
                    className={`event-item event-unknown${isHighlighted ? " event-highlight" : ""}`}
                  >
                    <div className="event-header">
                      <span className="event-role">{event.role}</span>
                      <span className="event-time">{formatTime(event.timestamp)}</span>
                    </div>
                    <div className="event-content">{event.content_text || "(no content)"}</div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </PageShell>
  );
}
