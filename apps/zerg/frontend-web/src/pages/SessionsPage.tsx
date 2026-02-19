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
import { useNavigate, useSearchParams, useLocation, Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { config } from "../lib/config";
import { useAgentSessions, useAgentFilters, useSemanticSearch } from "../hooks/useAgentSessions";
import {
  type AgentSession,
  type AgentSessionFilters,
  type SemanticSearchFilters,
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
import { parseUTC } from "../lib/dateUtils";
import "../styles/sessions.css";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DAYS_OPTIONS = [7, 14, 30, 60, 90] as const;
const PAGE_SIZE = 50;
const ENVIRONMENT_OPTIONS = ["production", "commis", "development", "test"] as const;

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
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
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

function groupSessionsByDay(sessions: AgentSession[]): Map<string, AgentSession[]> {
  const groups = new Map<string, AgentSession[]>();

  for (const session of sessions) {
    const key = getDateKey(session.last_activity_at || session.started_at);
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
  // Prefer LLM-generated title when available
  if (session.summary_title && session.summary_title !== "Untitled Session") {
    return session.summary_title;
  }

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

function formatDuration(startedAt: string, endedAt: string | null): string {
  if (!endedAt) return "In progress";
  const start = parseUTC(startedAt);
  const end = parseUTC(endedAt);
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
  highlightQuery?: string;
  isSemanticResult?: boolean;
}

function SessionCard({ session, onClick, highlightQuery, isSemanticResult }: SessionCardProps) {
  const turnCount = session.user_messages + session.assistant_messages;
  const toolCount = session.tool_calls;
  const isActive = !session.ended_at;

  const title = getSessionTitle(session);

  // Semantic results: show summary + similarity badge (not the raw "Similarity: 0.xxx" snippet)
  // Keyword results: show FTS match snippet with highlights
  const showSnippet = highlightQuery && session.match_snippet && !isSemanticResult;
  const showSummary = !showSnippet && session.summary;

  return (
    <Card className={`session-card${isActive ? " session-card--active" : ""}`} onClick={onClick}>
      <div className="session-card-header">
        <div className="session-card-provider">
          <span
            className="provider-dot"
            style={{ backgroundColor: getProviderColor(session.provider) }}
          />
          <span className="provider-name">{session.provider}</span>
          {session.environment && session.environment !== "production" && (
            <span className={`environment-badge environment-badge--${session.environment}`}>
              {session.environment}
            </span>
          )}
          {isActive && (
            <span className="session-active-indicator">In progress</span>
          )}
        </div>
        <span className="session-card-time">{formatRelativeTime(session.last_activity_at || session.started_at)}</span>
      </div>

      <div className="session-card-body">
        <div className="session-card-title">{title}</div>
        {showSummary && (
          <div className="session-card-summary">{session.summary}</div>
        )}
        {showSnippet && (
          <div className="session-card-snippet">
            {renderHighlightedText(session.match_snippet!, highlightQuery!)}
          </div>
        )}
        {isSemanticResult && session.match_snippet && (
          <div className="session-card-similarity">
            <Badge variant="neutral">{session.match_snippet}</Badge>
          </div>
        )}
        {session.git_branch && (
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
          <span className="session-stat-separator">&middot;</span>
          <span className="session-stat session-stat--secondary">Started {formatRelativeTime(session.started_at)}</span>
        </div>
        <div className="session-card-actions">
          {session.provider === "claude" && (
            <span className="session-card-resume-hint" title="Resume available">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
            </span>
          )}
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
  sessions: AgentSession[];
  onSessionClick: (session: AgentSession) => void;
  highlightQuery?: string;
  isSemanticResult?: boolean;
}

function SessionGroup({ title, sessions, onSessionClick, highlightQuery, isSemanticResult }: SessionGroupProps) {
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

  // Filter state from URL params
  const [project, setProject] = useState(searchParams.get("project") || "");
  const [provider, setProvider] = useState(searchParams.get("provider") || "");
  const [environment, setEnvironment] = useState(searchParams.get("environment") || "");
  const [daysBack, setDaysBack] = useState(
    Number(searchParams.get("days_back")) || 14
  );
  const [searchQuery, setSearchQuery] = useState(searchParams.get("query") || "");
  const [debouncedQuery, setDebouncedQuery] = useState(searchQuery);
  const [semanticMode, setSemanticMode] = useState(searchParams.get("semantic") === "1");

  // Collapsible filters — open by default if URL has active filters
  const hasUrlFilters = !!(
    searchParams.get("project") ||
    searchParams.get("provider") ||
    searchParams.get("environment") ||
    (searchParams.get("days_back") && Number(searchParams.get("days_back")) !== 14)
  );
  const [filtersOpen, setFiltersOpen] = useState(hasUrlFilters);

  // Pagination state
  const [limit, setLimit] = useState(PAGE_SIZE);

  // Fetch dynamic filter options
  const { data: filtersData, isLoading: filtersLoading } = useAgentFilters(daysBack);
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
    if (environment) params.set("environment", environment);
    if (daysBack !== 14) params.set("days_back", String(daysBack));
    if (debouncedQuery) params.set("query", debouncedQuery);
    if (semanticMode) params.set("semantic", "1");
    setSearchParams(params, { replace: true });
  }, [project, provider, environment, daysBack, debouncedQuery, semanticMode, setSearchParams]);

  // Reset pagination when filters change
  useEffect(() => {
    setLimit(PAGE_SIZE);
  }, [project, provider, environment, daysBack, debouncedQuery]);

  // Build filters
  const filters: AgentSessionFilters = useMemo(
    () => ({
      project: project || undefined,
      provider: provider || undefined,
      environment: environment || undefined,
      days_back: daysBack,
      query: debouncedQuery || undefined,
      limit,
    }),
    [project, provider, environment, daysBack, debouncedQuery, limit]
  );

  // Semantic search filters (only used when semantic mode is on + query present)
  const semanticFilters: SemanticSearchFilters = useMemo(
    () => ({
      query: debouncedQuery || "",
      project: project || undefined,
      provider: provider || undefined,
      environment: environment || undefined,
      days_back: daysBack,
      limit: Math.min(limit, 50),
    }),
    [debouncedQuery, project, provider, environment, daysBack, limit]
  );

  const useSemanticQuery = semanticMode && !!debouncedQuery;

  // Keyword search (default)
  const keywordResult = useAgentSessions(filters, {
    refetchInterval: 30_000,
    enabled: !useSemanticQuery,
  });

  // Semantic search (when toggled on + query present)
  const semanticResult = useSemanticSearch(semanticFilters, {
    enabled: useSemanticQuery,
  });

  // Merge results based on mode
  const activeResult = useSemanticQuery ? semanticResult : keywordResult;
  const data = activeResult.data;
  const isLoading = activeResult.isLoading;
  const error = activeResult.error;
  const refetch = activeResult.refetch;

  const sessions = useMemo(() => data?.sessions || [], [data?.sessions]);
  const total = data?.total || 0;
  const hasMore = !useSemanticQuery && sessions.length < total;

  // Group sessions by day
  const groupedSessions = useMemo(() => groupSessionsByDay(sessions), [sessions]);

  // Handle session click - preserve current filters in location state
  const handleSessionClick = useCallback((session: AgentSession) => {
    const matchId = debouncedQuery && session.match_event_id ? `?event_id=${session.match_event_id}` : "";
    navigate(`/timeline/${session.id}${matchId}`, {
      state: { from: location.pathname + location.search },
    });
  }, [navigate, location, debouncedQuery]);

  // Load more sessions
  const handleLoadMore = useCallback(() => {
    setLimit((prev) => prev + PAGE_SIZE);
  }, []);

  // Clear filters
  const handleClearFilters = useCallback(() => {
    setProject("");
    setProvider("");
    setEnvironment("");
    setDaysBack(14);
    setSearchQuery("");
    setSemanticMode(false);
    setFiltersOpen(false);
  }, []);


  // Demo seeding state
  const queryClient = useQueryClient();
  const [demoLoading, setDemoLoading] = useState(false);
  const [seedError, setSeedError] = useState<string | null>(null);

  const handleSeedDemo = useCallback(async () => {
    setDemoLoading(true);
    setSeedError(null);
    try {
      await seedDemoSessions();
      // Invalidate both sessions and filter options so new demo data appears
      queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
      queryClient.invalidateQueries({ queryKey: ["agent-session-filters"] });
    } catch {
      setSeedError("Failed to load demo sessions. Please try again.");
    } finally {
      setDemoLoading(false);
    }
  }, [queryClient]);

  const hasFilters = !!(project || provider || environment || daysBack !== 14 || searchQuery);
  const showGuidedEmptyState = sessions.length === 0 && !hasFilters;

  // Count active non-default filters (for badge)
  const activeFilterCount = [
    project,
    provider,
    environment,
    daysBack !== 14 ? "active" : "",
  ].filter(Boolean).length;

  // Ready signal for E2E
  useEffect(() => {
    if (!isLoading) {
      document.body.setAttribute("data-ready", "true");
      document.body.setAttribute("data-screenshot-ready", "true");
    }
    return () => {
      document.body.removeAttribute("data-ready");
      document.body.removeAttribute("data-screenshot-ready");
    };
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
          actions={total > 0 ? <span className="sessions-header-count">{total} sessions</span> : undefined}
        />

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
              placeholder={semanticMode ? "Semantic search..." : "Search timeline..."}
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="sessions-search-input"
            />
            <div
              className="sessions-search-mode"
              role="radiogroup"
              aria-label="Search mode"
              onKeyDown={(e) => {
                if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
                  e.preventDefault();
                  setSemanticMode((prev) => !prev);
                  // Move focus to the newly active button
                  const group = e.currentTarget;
                  requestAnimationFrame(() => {
                    const active = group.querySelector<HTMLButtonElement>('[aria-checked="true"]');
                    active?.focus();
                  });
                }
              }}
            >
              <button
                type="button"
                role="radio"
                aria-checked={!semanticMode}
                tabIndex={!semanticMode ? 0 : -1}
                className={`sessions-mode-btn${!semanticMode ? " sessions-mode-btn--active" : ""}`}
                onClick={() => setSemanticMode(false)}
              >
                Keyword
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={semanticMode}
                tabIndex={semanticMode ? 0 : -1}
                className={`sessions-mode-btn${semanticMode ? " sessions-mode-btn--active" : ""}`}
                onClick={() => setSemanticMode(true)}
                title="AI-powered similarity search using embeddings"
              >
                Semantic
              </button>
            </div>
          </div>
          <div className="sessions-toolbar-actions">
            <Button variant="ghost" size="sm" onClick={handleClearFilters} disabled={!hasFilters}>
              Clear
            </Button>
            <button
              type="button"
              className={`sessions-filter-toggle${filtersOpen ? " sessions-filter-toggle--open" : ""}`}
              onClick={() => setFiltersOpen((v) => !v)}
              aria-expanded={filtersOpen}
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

        {/* Collapsible Filter Panel */}
        {filtersOpen && (
          <div id="filter-panel" role="region" aria-label="Session filters" className="sessions-filter-panel">
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
            <FilterSelect
              label="environment"
              value={environment}
              options={[...ENVIRONMENT_OPTIONS]}
              onChange={setEnvironment}
            />
            <DaysSelect value={daysBack} onChange={setDaysBack} />
          </div>
        )}

        {/* Timeline List */}
        {sessions.length === 0 ? (
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
                isSemanticResult={useSemanticQuery}
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
