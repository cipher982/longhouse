/**
 * SessionsPage - Browse agent sessions shipped via the shipper
 *
 * Features:
 * - Sessions list grouped by day
 * - Filter by project, provider, date range
 * - Search sessions by content
 * - Live updates via polling
 * - Click to view session details
 */

import { useState, useEffect, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAgentSessions } from "../hooks/useAgentSessions";
import type { AgentSession, AgentSessionFilters } from "../services/api/agents";
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
import "../styles/sessions.css";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PROVIDERS = ["claude", "codex", "gemini"] as const;
const PROJECTS = ["zerg", "life-hub", "sauron", "hdr", "mytech"] as const;
const DAYS_OPTIONS = [7, 14, 30, 60, 90] as const;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatRelativeTime(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

function getDateKey(dateStr: string): string {
  const date = new Date(dateStr);
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

function groupSessionsByDay(sessions: AgentSession[]): Map<string, AgentSession[]> {
  const groups = new Map<string, AgentSession[]>();

  for (const session of sessions) {
    const key = getDateKey(session.started_at);
    const existing = groups.get(key) || [];
    existing.push(session);
    groups.set(key, existing);
  }

  return groups;
}

function getProviderColor(provider: string): string {
  switch (provider) {
    case "claude":
      return "var(--color-brand-accent)"; // Orange
    case "codex":
      return "var(--color-intent-success)"; // Green
    case "gemini":
      return "var(--color-neon-cyan)"; // Cyan
    default:
      return "var(--color-text-secondary)";
  }
}

function truncateMessage(msg: string | null, maxLen: number = 80): string {
  if (!msg) return "";
  if (msg.length <= maxLen) return msg;
  return msg.slice(0, maxLen) + "...";
}

function formatDuration(startedAt: string, endedAt: string | null): string {
  if (!endedAt) return "In progress";
  const start = new Date(startedAt);
  const end = new Date(endedAt);
  const diffMs = end.getTime() - start.getTime();
  const diffMins = Math.floor(diffMs / 60000);

  if (diffMins < 1) return "<1m";
  if (diffMins < 60) return `${diffMins}m`;
  const hours = Math.floor(diffMins / 60);
  const mins = diffMins % 60;
  return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
}

// ---------------------------------------------------------------------------
// Filter Components
// ---------------------------------------------------------------------------

interface FilterSelectProps {
  label: string;
  value: string;
  options: readonly string[];
  onChange: (value: string) => void;
}

function FilterSelect({ label, value, options, onChange }: FilterSelectProps) {
  return (
    <select
      className="sessions-filter-select"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
    >
      <option value="">All {label}s</option>
      {options.map((opt) => (
        <option key={opt} value={opt}>
          {opt}
        </option>
      ))}
    </select>
  );
}

interface DaysSelectProps {
  value: number;
  onChange: (value: number) => void;
}

function DaysSelect({ value, onChange }: DaysSelectProps) {
  return (
    <select
      className="sessions-filter-select"
      value={value}
      onChange={(e) => onChange(Number(e.target.value))}
      aria-label="Days back"
    >
      {DAYS_OPTIONS.map((days) => (
        <option key={days} value={days}>
          Last {days} days
        </option>
      ))}
    </select>
  );
}

// ---------------------------------------------------------------------------
// Session Card Component
// ---------------------------------------------------------------------------

interface SessionCardProps {
  session: AgentSession;
  onClick: () => void;
}

function SessionCard({ session, onClick }: SessionCardProps) {
  const turnCount = session.user_messages + session.assistant_messages;
  const toolCount = session.tool_calls;

  // Use git branch or project as title, fallback to cwd
  const title = session.project || session.git_branch || session.cwd?.split("/").pop() || "Session";

  return (
    <Card className="session-card" onClick={onClick}>
      <div className="session-card-header">
        <div className="session-card-provider">
          <span
            className="provider-dot"
            style={{ backgroundColor: getProviderColor(session.provider) }}
          />
          <span className="provider-name">{session.provider}</span>
        </div>
        <span className="session-card-time">{formatRelativeTime(session.started_at)}</span>
      </div>

      <div className="session-card-body">
        <div className="session-card-title">{title}</div>
        {session.git_branch && session.project && (
          <div className="session-card-branch">
            <span className="branch-icon">&#x2387;</span>
            {session.git_branch}
          </div>
        )}
      </div>

      <div className="session-card-footer">
        <div className="session-card-stats">
          <span className="session-stat">{turnCount} turns</span>
          <span className="session-stat-separator">&middot;</span>
          <span className="session-stat">{toolCount} tools</span>
          {session.ended_at && (
            <>
              <span className="session-stat-separator">&middot;</span>
              <span className="session-stat">{formatDuration(session.started_at, session.ended_at)}</span>
            </>
          )}
        </div>
        <span className="session-card-arrow">&rarr;</span>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Session Group Component
// ---------------------------------------------------------------------------

interface SessionGroupProps {
  title: string;
  sessions: AgentSession[];
  onSessionClick: (session: AgentSession) => void;
}

function SessionGroup({ title, sessions, onSessionClick }: SessionGroupProps) {
  return (
    <div className="session-group">
      <div className="session-group-header">
        <span className="session-group-title">{title}</span>
        <Badge variant="neutral">{sessions.length}</Badge>
      </div>
      <div className="session-group-list">
        {sessions.map((session) => (
          <SessionCard
            key={session.id}
            session={session}
            onClick={() => onSessionClick(session)}
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
  const [searchParams, setSearchParams] = useSearchParams();

  // Filter state from URL params
  const [project, setProject] = useState(searchParams.get("project") || "");
  const [provider, setProvider] = useState(searchParams.get("provider") || "");
  const [daysBack, setDaysBack] = useState(
    Number(searchParams.get("days_back")) || 14
  );
  const [searchQuery, setSearchQuery] = useState(searchParams.get("query") || "");
  const [debouncedQuery, setDebouncedQuery] = useState(searchQuery);

  // Debounce search query
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedQuery(searchQuery);
    }, 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  // Update URL params when filters change
  useEffect(() => {
    const params = new URLSearchParams();
    if (project) params.set("project", project);
    if (provider) params.set("provider", provider);
    if (daysBack !== 14) params.set("days_back", String(daysBack));
    if (debouncedQuery) params.set("query", debouncedQuery);
    setSearchParams(params, { replace: true });
  }, [project, provider, daysBack, debouncedQuery, setSearchParams]);

  // Build filters
  const filters: AgentSessionFilters = useMemo(
    () => ({
      project: project || undefined,
      provider: provider || undefined,
      days_back: daysBack,
      query: debouncedQuery || undefined,
      limit: 100,
    }),
    [project, provider, daysBack, debouncedQuery]
  );

  // Fetch sessions with polling
  const { data, isLoading, error, refetch } = useAgentSessions(filters, {
    refetchInterval: 30_000, // Refresh every 30s
  });

  const sessions = data?.sessions || [];

  // Group sessions by day
  const groupedSessions = useMemo(() => groupSessionsByDay(sessions), [sessions]);

  // Handle session click
  const handleSessionClick = (session: AgentSession) => {
    navigate(`/sessions/${session.id}`);
  };

  // Clear filters
  const handleClearFilters = () => {
    setProject("");
    setProvider("");
    setDaysBack(14);
    setSearchQuery("");
  };

  const hasFilters = project || provider || daysBack !== 14 || searchQuery;

  // Ready signal for E2E
  useEffect(() => {
    if (!isLoading) {
      document.body.setAttribute("data-ready", "true");
    }
    return () => document.body.removeAttribute("data-ready");
  }, [isLoading]);

  // Loading state
  if (isLoading) {
    return (
      <PageShell size="wide" className="sessions-page-container">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading sessions..."
          description="Fetching your agent sessions."
        />
      </PageShell>
    );
  }

  // Error state
  if (error) {
    return (
      <PageShell size="wide" className="sessions-page-container">
        <EmptyState
          variant="error"
          title="Error loading sessions"
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

  return (
    <PageShell size="wide" className="sessions-page-container">
      <div className="sessions-page">
        <SectionHeader
          title="Agent Sessions"
          description="Browse and review your AI coding sessions."
        />

        {/* Filter Bar */}
        <div className="sessions-filter-bar">
          <div className="sessions-filters">
            <FilterSelect
              label="project"
              value={project}
              options={PROJECTS}
              onChange={setProject}
            />
            <FilterSelect
              label="provider"
              value={provider}
              options={PROVIDERS}
              onChange={setProvider}
            />
            <DaysSelect value={daysBack} onChange={setDaysBack} />
          </div>
          <div className="sessions-search">
            <Input
              type="search"
              placeholder="Search sessions..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="sessions-search-input"
            />
            {hasFilters && (
              <Button variant="ghost" size="sm" onClick={handleClearFilters}>
                Clear
              </Button>
            )}
          </div>
        </div>

        {/* Sessions List */}
        {sessions.length === 0 ? (
          <EmptyState
            title="No sessions found"
            description={
              hasFilters
                ? "Try adjusting your filters or search query."
                : "Sessions will appear here once the shipper starts syncing."
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
              />
            ))}
          </div>
        )}

        {/* Total count */}
        {data && data.total > 0 && (
          <div className="sessions-footer">
            <span className="sessions-count">
              Showing {sessions.length} of {data.total} sessions
            </span>
          </div>
        )}
      </div>
    </PageShell>
  );
}
