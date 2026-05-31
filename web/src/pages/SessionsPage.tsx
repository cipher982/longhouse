/**
 * TimelinePage - Browse agent sessions shipped via the shipper
 *
 * Features:
 * - Inbox layout grouped by repo (Active above, Closed below)
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
import { useDocumentVisible } from "../hooks/useDocumentVisible";
import { useTimelineSessionStream } from "../hooks/useTimelineSessionStream";
import { useReadinessFlag } from "../lib/readiness-contract";
import {
  type AgentSessionFilters,
  fetchAgentSessionWorkspace,
  type TimelineSessionCard,
  seedDemoSessions,
} from "../services/api/agents";
import {
  Button,
  EmptyState,
  PageShell,
  Spinner,
  Input,
} from "../components/ui";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { RecallPanel } from "../components/RecallPanel";
import { TimelineInbox } from "../components/sessions/TimelineInbox";
import { InboxTuner } from "../components/sessions/InboxTuner";
import { FilterChip, FilterPopover } from "../components/sessions/SessionsFilter";
import LaunchSessionModal from "../components/LaunchSessionModal";
import {
  type SortOrder,
  type SessionsUrlState,
  buildSessionDetailPath,
  readSessionsUrlState,
  buildSessionsSearchParams,
} from "../lib/sessionUtils";
import "../styles/sessions.css";
import "../styles/inbox.css";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PAGE_SIZE = 50;
const DEFAULT_DAYS_BACK = 14;
const TIMELINE_RECONCILIATION_MS = 120_000;
const DEFAULT_SORT_ORDER = "relevant";
const SESSION_WORKSPACE_PREFETCH_LIMIT = 200;
const SESSION_CARD_SCROLL_SUPPRESSION_MS = 250;

// ---------------------------------------------------------------------------
// useRelativeTimeClock
// ---------------------------------------------------------------------------

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
    deviceId,
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
  const lastTimelineScrollAtRef = useRef(0);
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
  const handleDeviceIdChange = useCallback(
    (value: string) => updateFilterState({ deviceId: value }),
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
      device_id: deviceId || undefined,
      days_back: daysBack,
      query: debouncedQuery || undefined,
      limit,
      mode: aiSearch ? "hybrid" : undefined,
      sort: debouncedQuery ? (sortOrder === "recent" ? "recency" : "relevance") : undefined,
      hide_autonomous: hideAutonomous ? undefined : false,
    }),
    [project, provider, deviceId, daysBack, debouncedQuery, limit, aiSearch, sortOrder, hideAutonomous]
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
    timelineStreamEnabled &&
    !timelineStreamBootstrapKeysRef.current.has(timelineStreamBootstrapKey) &&
    (data?.sessions.length ?? 0) > 0;

  useEffect(() => {
    if (!timelineStreamEnabled) {
      return;
    }
    timelineStreamBootstrapKeysRef.current.add(timelineStreamBootstrapKey);
  }, [timelineStreamBootstrapKey, timelineStreamEnabled]);

  const sessions = useMemo(() => data?.sessions || [], [data?.sessions]);
  const total = data?.total || 0;
  const hasRealSessions = data?.has_real_sessions ?? true;
  const groupedQueryMode = data?.query_grouping_mode === "grouped_results";
  const hasMore = groupedQueryMode ? (data?.query_grouping_has_more ?? false) : sessions.length < total;

  useTimelineSessionStream(filters, {
    enabled: timelineStreamEnabled,
    skipInitialReplay: skipInitialTimelineReplay,
  });

  const threadCards = sessions;

  // PageShell owns scroll detection and CSS class toggling; we only track the
  // timestamp here to gate hover-prefetch intent.
  const handleScrollActivity = useCallback(() => {
    lastTimelineScrollAtRef.current = performance.now();
  }, []);

  const allowHoverPrefetch = useCallback(() => {
    return performance.now() - lastTimelineScrollAtRef.current > SESSION_CARD_SCROLL_SUPPRESSION_MS;
  }, []);

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

  const [launchModalOpen, setLaunchModalOpen] = useState(false);
  const headerActions = (
    <div className="sessions-header-actions">
      {threadCards.length > 0 && (
        <span className="sessions-header-count">
          {groupedQueryMode ? `${threadCards.length} results` : `${threadCards.length} tasks`}
        </span>
      )}
      <Button
        variant="primary"
        size="sm"
        onClick={() => setLaunchModalOpen(true)}
        data-testid="sessions-start-session"
      >
        Start session
      </Button>
    </div>
  );

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
      deviceId: "",
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
  const autoSeededRef = useRef(false);

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

  const hasFilters = !!(project || provider || deviceId || daysBack !== DEFAULT_DAYS_BACK || searchQuery);
  const showGuidedEmptyState = sessions.length === 0 && !hasFilters;

  // Auto-seed demo sessions on first empty load so new users see a populated
  // timeline immediately rather than a blank screen.
  useEffect(() => {
    if (!isLoading && !autoSeededRef.current && showGuidedEmptyState && !config.demoMode) {
      autoSeededRef.current = true;
      handleSeedDemo();
    }
  }, [isLoading, showGuidedEmptyState, handleSeedDemo]);

  // Count active non-default filters (for badge)
  const activeFilterCount = [
    project,
    provider,
    deviceId,
    daysBack !== DEFAULT_DAYS_BACK ? "active" : "",
    !hideAutonomous ? "active" : "",
  ].filter(Boolean).length;

  // Ready signal for E2E
  useReadinessFlag({ ready: !isLoading, screenshotReady: !isLoading });

  // Loading state
  if (isLoading && sessions.length === 0) {
    return (
      <PageShell size="wide" className="sessions-page-container" onScrollActivity={handleScrollActivity}>
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
      <PageShell size="wide" className="sessions-page-container" onScrollActivity={handleScrollActivity}>
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
      <PageShell size="wide" className="sessions-page-container" onScrollActivity={handleScrollActivity}>
        <div className="sessions-hero-empty">
          <EmptyState
            icon={demoLoading ? <Spinner size="lg" /> : undefined}
            title="Connect your first machine"
            description="Run one command on the machine where you use Claude Code, Codex, or Antigravity and your sessions will start appearing here."
            action={
              <div className="sessions-guided-actions">
                <Button
                  variant="primary"
                  size="md"
                  onClick={() => navigate("/docs/quickstart")}
                >
                  See setup steps
                </Button>
                <Button
                  variant="secondary"
                  size="md"
                  onClick={() => setLaunchModalOpen(true)}
                  data-testid="timeline-empty-start-session"
                >
                  Start session
                </Button>
                <Button
                  variant="secondary"
                  size="md"
                  onClick={() => navigate("/runners")}
                  data-testid="timeline-empty-runner-action"
                >
                  Machines
                </Button>
                {seedError && (
                  <Button
                    variant="secondary"
                    size="md"
                    onClick={handleSeedDemo}
                    disabled={demoLoading}
                  >
                    Retry demo sessions
                  </Button>
                )}
                {seedError && (
                  <p style={{ color: "var(--color-intent-error)", marginTop: "0.5rem", fontSize: "0.875rem" }}>
                    {seedError}
                  </p>
                )}
              </div>
            }
          />
          <div className="sessions-guided-steps">
            <p className="sessions-guided-steps-label">Run this on your machine:</p>
            <ol className="sessions-guided-steps-list">
              <li><code>curl -fsSL https://get.longhouse.ai/install.sh | bash</code> &mdash; install the CLI</li>
              <li><code>longhouse connect --install</code> &mdash; link this machine and start background import</li>
              <li><code>longhouse ship</code> &mdash; pull your existing sessions in now</li>
            </ol>
            <p className="sessions-guided-cli-hint">
              Works with{" "}
              <a href="https://docs.anthropic.com/en/docs/claude-code/overview" target="_blank" rel="noopener noreferrer">Claude Code</a>,{" "}
              <a href="https://github.com/openai/codex" target="_blank" rel="noopener noreferrer">Codex CLI</a>, and{" "}
              <a href="https://antigravity.google/product/antigravity-cli" target="_blank" rel="noopener noreferrer">Antigravity CLI</a>.
              {demoLoading && " Demo sessions are loading in the background."}
            </p>
          </div>
        </div>
        <LaunchSessionModal
          isOpen={launchModalOpen}
          onClose={() => setLaunchModalOpen(false)}
          onLaunched={(sessionId) => {
            setLaunchModalOpen(false);
            navigate(`/timeline/${sessionId}`);
          }}
        />
      </PageShell>
    );
  }

  return (
    <PageShell size="wide" className="sessions-page-container" onScrollActivity={handleScrollActivity}>
      <div className="sessions-page">
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
              Import real sessions with <code>longhouse connect --install</code> and <code>longhouse ship</code>,
              then launch managed sessions with Longhouse when you want control after launch.
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
                  if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
                    e.preventDefault();
                    handleSortOrderChange(orders[(idx - 1 + orders.length) % orders.length]);
                  }
                  if (e.key === "ArrowRight" || e.key === "ArrowDown") {
                    e.preventDefault();
                    handleSortOrderChange(orders[(idx + 1) % orders.length]);
                  }
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
          {(provider || deviceId || project || daysBack !== DEFAULT_DAYS_BACK || !hideAutonomous) && (
            <div className="sessions-filter-chips">
              {provider && <FilterChip label={provider} onDismiss={() => handleProviderChange("")} />}
              {deviceId && <FilterChip label={deviceId} onDismiss={() => handleDeviceIdChange("")} />}
              {project && <FilterChip label={project} onDismiss={() => handleProjectChange("")} />}
              {daysBack !== DEFAULT_DAYS_BACK && <FilterChip label={`${daysBack}d`} onDismiss={() => handleDaysBackChange(DEFAULT_DAYS_BACK)} />}
              {!hideAutonomous && <FilterChip label="show auto" onDismiss={() => handleHideAutonomousChange(true)} />}
            </div>
          )}

          <div className="sessions-toolbar-actions">
            {headerActions}
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
            deviceId={deviceId} setDeviceId={handleDeviceIdChange} machineOptions={machineOptions}
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

        {/* Timeline Inbox */}
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
          <TimelineInbox
            sessions={threadCards}
            onSessionClick={handleSessionClick}
            onSessionPrefetch={handleSessionPrefetch}
            allowHoverPrefetch={allowHoverPrefetch}
            relativeNowMs={relativeNowMs}
            highlightQuery={debouncedQuery}
          />
        )}

        {/* Footer with count and load more */}
        {total > 0 && (
          <div className="sessions-footer">
            <span className="sessions-count">
              {groupedQueryMode
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
      <LaunchSessionModal
        isOpen={launchModalOpen}
        onClose={() => setLaunchModalOpen(false)}
        onLaunched={(sessionId) => {
          setLaunchModalOpen(false);
          navigate(`/timeline/${sessionId}`);
        }}
      />
      <InboxTuner />
    </PageShell>
  );
}
