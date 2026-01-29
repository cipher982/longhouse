/**
 * SessionDetailPage - View detailed event timeline for an agent session
 *
 * Features:
 * - Full event timeline (user, assistant, tool)
 * - Collapsible tool calls (collapsed by default)
 * - Session metadata header
 * - Back navigation
 */

import { useState, useEffect, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useAgentSession, useAgentSessionEvents } from "../hooks/useAgentSessions";
import type { AgentEvent } from "../services/api/agents";
import {
  Button,
  Badge,
  SectionHeader,
  EmptyState,
  PageShell,
  Spinner,
} from "../components/ui";
import "../styles/sessions.css";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatTime(dateStr: string): string {
  return new Date(dateStr).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatFullDate(dateStr: string): string {
  return new Date(dateStr).toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDuration(startedAt: string, endedAt: string | null): string {
  if (!endedAt) return "In progress";
  const start = new Date(startedAt);
  const end = new Date(endedAt);
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
      return { icon: "$", color: "var(--color-intent-warning)" };
    case "read":
      return { icon: "R", color: "var(--color-neon-cyan)" };
    case "write":
      return { icon: "W", color: "var(--color-intent-success)" };
    case "edit":
      return { icon: "E", color: "var(--color-brand-primary)" };
    case "grep":
    case "glob":
      return { icon: "?", color: "var(--color-text-secondary)" };
    case "task":
      return { icon: "T", color: "var(--color-neon-secondary)" };
    default:
      return { icon: toolName[0]?.toUpperCase() || "?", color: "var(--color-text-secondary)" };
  }
}

// ---------------------------------------------------------------------------
// Event Components
// ---------------------------------------------------------------------------

interface UserMessageProps {
  event: AgentEvent;
}

function UserMessage({ event }: UserMessageProps) {
  return (
    <div className="event-item event-user">
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
}

function AssistantMessage({ event }: AssistantMessageProps) {
  return (
    <div className="event-item event-assistant">
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

interface ToolCallProps {
  event: AgentEvent;
  isExpanded: boolean;
  onToggle: () => void;
}

function ToolCall({ event, isExpanded, onToggle }: ToolCallProps) {
  const toolInfo = getToolDisplayInfo(event.tool_name || "");
  const hasInput = event.tool_input_json && Object.keys(event.tool_input_json).length > 0;
  const hasOutput = event.tool_output_text && event.tool_output_text.length > 0;

  // Extract brief summary for collapsed state
  const getSummary = (): string => {
    if (!event.tool_input_json) return "";
    const input = event.tool_input_json;

    // Common patterns
    if ("file_path" in input) return truncatePath(String(input.file_path));
    if ("command" in input) return String(input.command).slice(0, 60);
    if ("pattern" in input) return String(input.pattern);
    if ("path" in input) return truncatePath(String(input.path));
    if ("url" in input) return String(input.url).slice(0, 50);

    return "";
  };

  const summary = getSummary();

  return (
    <div className={`event-item event-tool ${isExpanded ? "expanded" : ""}`}>
      <button
        className="event-tool-header"
        onClick={onToggle}
        aria-expanded={isExpanded}
      >
        <div className="event-tool-title">
          <span className="tool-icon" style={{ backgroundColor: toolInfo.color }}>
            {toolInfo.icon}
          </span>
          <span className="tool-name">{event.tool_name}</span>
          {!isExpanded && summary && (
            <span className="tool-summary">{summary}</span>
          )}
        </div>
        <div className="event-tool-meta">
          <span className="event-time">{formatTime(event.timestamp)}</span>
          <span className="expand-icon">{isExpanded ? "▼" : "▶"}</span>
        </div>
      </button>

      {isExpanded && (
        <div className="event-tool-body">
          {hasInput && (
            <div className="tool-section">
              <div className="tool-section-label">Input</div>
              <pre className="tool-section-content">
                {JSON.stringify(event.tool_input_json, null, 2)}
              </pre>
            </div>
          )}
          {hasOutput && (
            <div className="tool-section">
              <div className="tool-section-label">Output</div>
              <pre className="tool-section-content tool-output">
                {event.tool_output_text}
              </pre>
            </div>
          )}
          {!hasInput && !hasOutput && (
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

  // Fetch session and events
  const { data: session, isLoading: sessionLoading, error: sessionError } = useAgentSession(sessionId || null);
  const { data: eventsData, isLoading: eventsLoading, error: eventsError } = useAgentSessionEvents(sessionId || null, {
    limit: 1000,
  });

  const events = eventsData?.events || [];

  // Expanded state for tool calls
  const [expandedTools, setExpandedTools] = useState<Set<number>>(new Set());

  // Toggle individual tool
  const toggleTool = (eventId: number) => {
    setExpandedTools((prev) => {
      const next = new Set(prev);
      if (next.has(eventId)) {
        next.delete(eventId);
      } else {
        next.add(eventId);
      }
      return next;
    });
  };

  // Expand/collapse all tools
  const toolEvents = useMemo(
    () => events.filter((e) => e.role === "tool"),
    [events]
  );
  const allExpanded = toolEvents.length > 0 && toolEvents.every((e) => expandedTools.has(e.id));

  const toggleAll = () => {
    if (allExpanded) {
      setExpandedTools(new Set());
    } else {
      setExpandedTools(new Set(toolEvents.map((e) => e.id)));
    }
  };

  // Ready signal for E2E
  useEffect(() => {
    if (!sessionLoading && !eventsLoading) {
      document.body.setAttribute("data-ready", "true");
    }
    return () => document.body.removeAttribute("data-ready");
  }, [sessionLoading, eventsLoading]);

  // Back navigation
  const handleBack = () => {
    navigate("/sessions");
  };

  // Loading state
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

  // Error state
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
              Back to Sessions
            </Button>
          }
        />
      </PageShell>
    );
  }

  const title = session.project || session.git_branch || "Session";
  const turnCount = session.user_messages + session.assistant_messages;

  return (
    <PageShell size="wide" className="sessions-page-container">
      <div className="session-detail-page">
        {/* Header */}
        <div className="session-detail-header">
          <Button variant="ghost" onClick={handleBack} className="back-button">
            &larr; Back
          </Button>
          <SectionHeader
            title={title}
            description={session.cwd ? truncatePath(session.cwd, 80) : undefined}
            actions={
              toolEvents.length > 0 ? (
                <Button variant="ghost" size="sm" onClick={toggleAll}>
                  {allExpanded ? "Collapse All" : "Expand All"}
                </Button>
              ) : undefined
            }
          />
        </div>

        {/* Metadata */}
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
            <span className="meta-label">Started</span>
            <span className="meta-value">{formatFullDate(session.started_at)}</span>
          </div>
          <span className="meta-separator">&middot;</span>
          <div className="meta-item">
            <span className="meta-label">Duration</span>
            <span className="meta-value">
              {formatDuration(session.started_at, session.ended_at)}
            </span>
          </div>
          <span className="meta-separator">&middot;</span>
          <div className="meta-item">
            <Badge variant="neutral">{turnCount} turns</Badge>
            <Badge variant="neutral">{session.tool_calls} tools</Badge>
          </div>
        </div>

        {/* Git info */}
        {(session.git_branch || session.git_repo) && (
          <div className="session-detail-git">
            {session.git_branch && (
              <span className="git-branch">
                <span className="branch-icon">&#x2387;</span>
                {session.git_branch}
              </span>
            )}
            {session.git_repo && (
              <span className="git-repo">{session.git_repo}</span>
            )}
          </div>
        )}

        {/* Event Timeline */}
        <div className="session-timeline">
          <div className="timeline-header">
            <span className="timeline-title">Event Timeline</span>
            <span className="timeline-count">{events.length} events</span>
          </div>

          {events.length === 0 ? (
            <EmptyState
              title="No events"
              description="This session has no recorded events."
            />
          ) : (
            <div className="timeline-events">
              {events.map((event) => {
                if (event.role === "user") {
                  return <UserMessage key={event.id} event={event} />;
                }
                if (event.role === "assistant") {
                  return <AssistantMessage key={event.id} event={event} />;
                }
                if (event.role === "tool") {
                  return (
                    <ToolCall
                      key={event.id}
                      event={event}
                      isExpanded={expandedTools.has(event.id)}
                      onToggle={() => toggleTool(event.id)}
                    />
                  );
                }
                // Unknown role - render as generic
                return (
                  <div key={event.id} className="event-item event-unknown">
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
