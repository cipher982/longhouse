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

import { useState, useEffect, useMemo, useCallback } from "react";
import { useNavigate, useSearchParams, useLocation } from "react-router-dom";
import { useAgentSessions, useAgentFilters } from "../hooks/useAgentSessions";
import {
  type AgentSession,
  type AgentSessionFilters,
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
import "../styles/sessions.css";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DAYS_OPTIONS = [7, 14, 30, 60, 90] as const;
const PAGE_SIZE = 50;

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

function isValidTitle(name: string | null | undefined): name is string {
  if (!name) return false;
  // Skip tmp folders, random hashes, and very short names
  if (name.startsWith("tmp") || /^[a-z0-9]{8,}$/i.test(name) || name.length < 3) {
    return false;
  }
  return true;
}

function getSessionTitle(session: AgentSession): string {
  if (isValidTitle(session.project)) return session.project;
  if (isValidTitle(session.git_branch)) return session.git_branch;

  // Try cwd folder
  if (session.cwd) {
    const folder = session.cwd.split("/").pop();
    if (isValidTitle(folder)) return folder;
  }

  // Fallback: "Claude session" (capitalized provider)
  return `${session.provider.charAt(0).toUpperCase() + session.provider.slice(1)} session`;
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
  options: string[];
  onChange: (value: string) => void;
  loading?: boolean;
}

function FilterSelect({ label, value, options, onChange, loading }: FilterSelectProps) {
  return (
    <select
      className="sessions-filter-select"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
      disabled={loading}
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

  const title = getSessionTitle(session);

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
          <span className="session-stat">{turnCount} {turnCount === 1 ? 'turn' : 'turns'}</span>
          <span className="session-stat-separator">&middot;</span>
          <span className="session-stat">{toolCount} {toolCount === 1 ? 'tool' : 'tools'}</span>
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
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();

  // Filter state from URL params
  const [project, setProject] = useState(searchParams.get("project") || "");
  const [provider, setProvider] = useState(searchParams.get("provider") || "");
  const [daysBack, setDaysBack] = useState(
    Number(searchParams.get("days_back")) || 14
  );
  const [searchQuery, setSearchQuery] = useState(searchParams.get("query") || "");
  const [debouncedQuery, setDebouncedQuery] = useState(searchQuery);

  // Pagination state
  const [limit, setLimit] = useState(PAGE_SIZE);

  // Fetch dynamic filter options
  const { data: filtersData, isLoading: filtersLoading, refetch: refetchFilters } = useAgentFilters(daysBack);
  const projectOptions = filtersData?.projects || [];
  const providerOptions = filtersData?.providers || [];

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

  // Reset pagination when filters change
  useEffect(() => {
    setLimit(PAGE_SIZE);
  }, [project, provider, daysBack, debouncedQuery]);

  // Build filters
  const filters: AgentSessionFilters = useMemo(
    () => ({
      project: project || undefined,
      provider: provider || undefined,
      days_back: daysBack,
      query: debouncedQuery || undefined,
      limit,
    }),
    [project, provider, daysBack, debouncedQuery, limit]
  );

  // Fetch sessions with polling
  const { data, isLoading, error, refetch } = useAgentSessions(filters, {
    refetchInterval: 30_000, // Refresh every 30s
  });

  const sessions = data?.sessions || [];
  const total = data?.total || 0;
  const hasMore = sessions.length < total;

  // Group sessions by day
  const groupedSessions = useMemo(() => groupSessionsByDay(sessions), [sessions]);

  // Handle session click - preserve current filters in location state
  const handleSessionClick = useCallback((session: AgentSession) => {
    navigate(`/timeline/${session.id}`, {
      state: { from: location.pathname + location.search },
    });
  }, [navigate, location]);

  // Load more sessions
  const handleLoadMore = useCallback(() => {
    setLimit((prev) => prev + PAGE_SIZE);
  }, []);

  // Clear filters
  const handleClearFilters = useCallback(() => {
    setProject("");
    setProvider("");
    setDaysBack(14);
    setSearchQuery("");
  }, []);


  const hasFilters = project || provider || daysBack !== 14 || searchQuery;
  const showGuidedEmptyState = sessions.length === 0 && !hasFilters;

  // Ready signal for E2E
  useEffect(() => {
    if (!isLoading) {
      document.body.setAttribute("data-ready", "true");
    }
    return () => document.body.removeAttribute("data-ready");
  }, [isLoading]);

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

  // Error state
  if (error) {
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

  return (
    <PageShell size="wide" className="sessions-page-container">
      <div className="sessions-page">
        <SectionHeader
          title="Timeline"
          description="A unified view of your AI coding sessions across providers."
        />

        {/* Filter Bar */}
        <div className="sessions-filter-bar">
          <div className="sessions-filters">
            <FilterSelect
              label="project"
              value={project}
              options={projectOptions}
              onChange={setProject}
              loading={filtersLoading}
            />
            <FilterSelect
              label="provider"
              value={provider}
              options={providerOptions}
              onChange={setProvider}
              loading={filtersLoading}
            />
            <DaysSelect value={daysBack} onChange={setDaysBack} />
          </div>
          <div className="sessions-search">
            <Input
              type="search"
              placeholder="Search timeline..."
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

        {/* Timeline List */}
        {showGuidedEmptyState ? (
          <EmptyState
            title="No sessions yet"
            description="Sessions sync from Claude Code. Run 'longhouse ship' to sync now."
          />
        ) : sessions.length === 0 ? (
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
              />
            ))}
          </div>
        )}

        {/* Footer with count and load more */}
        {total > 0 && (
          <div className="sessions-footer">
            <span className="sessions-count">
              Showing {sessions.length} of {total} sessions
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
