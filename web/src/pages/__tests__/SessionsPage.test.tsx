import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes, useLocation } from "react-router-dom";
import * as reactRouterDom from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as agentsApi from "../../services/api/agents";
import type {
  AgentSession,
  AgentSessionFilters,
  SessionCapabilities,
  SessionRuntimeDisplay,
  TimelineCardPresentation,
  TimelineSessionCard,
  TimelineSessionsListResponse,
} from "../../services/api/agents";
import type { Runner } from "../../services/api";
import { TestRouter } from "../../test/test-utils";
import SessionsPage from "../SessionsPage";

const hookMocks = vi.hoisted(() => ({
  useAgentSessions: vi.fn(),
  useAgentFilters: vi.fn(),
}));

const runnerHookMocks = vi.hoisted(() => ({
  useRunners: vi.fn(),
}));

const timelineStreamMocks = vi.hoisted(() => ({
  useTimelineSessionStream: vi.fn(),
}));

vi.mock("../../hooks/useAgentSessions", () => ({
  useAgentSessions: hookMocks.useAgentSessions,
  useAgentFilters: hookMocks.useAgentFilters,
}));

vi.mock("../../hooks/useRunners", () => ({
  useRunners: runnerHookMocks.useRunners,
}));

vi.mock("../../hooks/useTimelineSessionStream", () => ({
  useTimelineSessionStream: timelineStreamMocks.useTimelineSessionStream,
}));

vi.mock("../../lib/readiness-contract", () => ({
  useReadinessFlag: vi.fn(),
}));

vi.mock("../../lib/config", () => ({
  config: {
    llmAvailable: true,
  },
}));

const { useAgentSessions: mockUseAgentSessions, useAgentFilters: mockUseAgentFilters } = hookMocks;
const { useRunners: mockUseRunners } = runnerHookMocks;
const { useTimelineSessionStream: mockUseTimelineSessionStream } = timelineStreamMocks;

function makeCapabilities(overrides: Partial<SessionCapabilities> = {}): SessionCapabilities {
  return {
    live_control_available: false,
    host_reattach_available: false,
    reply_to_live_session_available: false,
    ...overrides,
  };
}

function makeRuntimeDisplay(overrides: Partial<SessionRuntimeDisplay> = {}): SessionRuntimeDisplay {
  return {
    truth_tier: "none",
    signal_tier: "none",
    state: null,
    tone: "inactive",
    headline: "Inactive",
    detail: null,
    phase_label: "Inactive",
    compact_tool_label: null,
    is_live: false,
    is_executing: false,
    needs_attention: false,
    is_idle: false,
    is_stalled: false,
    is_managed_local_truth: false,
    has_signal: false,
    control_path: "unmanaged",
    activity_recency: "none",
    lifecycle: "open",
    host_state: "unknown",
    terminal_reason: null,
    ...overrides,
  };
}

function makeTimelinePresentation(overrides: Partial<TimelineCardPresentation> = {}): TimelineCardPresentation {
  return {
    ownership: { label: "Unmanaged", tone: "neutral" },
    status: { label: "No live signal", tone: "inactive", seen_at: null, seen_at_prefix: "Checked" },
    border_tone: "inactive",
    ...overrides,
  };
}

function makeSession(overrides: Partial<AgentSession> = {}): AgentSession {
  const now = "2026-03-21T12:00:00Z";
  return {
    id: "session-1",
    provider: "codex",
    project: "zerg",
    device_id: "device-1",
    environment: "laptop",
    cwd: "/Users/example/git/zerg",
    git_repo: "https://github.com/cipher982/longhouse.git",
    git_branch: "main",
    started_at: now,
    ended_at: now,
    last_activity_at: now,
    user_messages: 4,
    assistant_messages: 4,
    tool_calls: 2,
    summary: "Shipped session cleanup.",
    summary_title: "Cleanup sessions page",
    first_user_message: "clean this up",
    match_event_id: null,
    match_snippet: null,
    match_role: null,
    match_score: null,
    thread_root_session_id: "session-1",
    thread_head_session_id: "session-1",
    thread_continuation_count: 1,
    continued_from_session_id: null,
    continuation_kind: null,
    origin_label: "laptop",
    home_label: null,
    branched_from_event_id: null,
    is_writable_head: true,
    control: null,
    capabilities: makeCapabilities(),
    runtime_display: makeRuntimeDisplay(),
    timeline_card: makeTimelinePresentation(),
    loop_mode: "assist",
    ...overrides,
  };
}

function makeTimelineCard(
  overrides: Partial<AgentSession> = {},
  cardOverrides: Partial<TimelineSessionCard> = {},
): TimelineSessionCard {
  const detail = makeSession(overrides);
  const headOverrides =
    cardOverrides.head != null
      ? cardOverrides.head
      : makeSession({
          ...overrides,
          id: detail.thread_head_session_id || detail.id,
        });
  const rootOverrides =
    cardOverrides.root != null
      ? cardOverrides.root
      : makeSession({
          ...overrides,
          id: detail.thread_root_session_id || detail.id,
        });

  return {
    thread_id: detail.thread_root_session_id,
    timeline_anchor_at: detail.timeline_anchor_at || detail.last_activity_at || detail.started_at,
    head: headOverrides,
    detail,
    root: rootOverrides,
    continuation_count: detail.thread_continuation_count,
    started_origin_label: rootOverrides.origin_label || rootOverrides.environment,
    head_origin_label: headOverrides.origin_label || headOverrides.environment,
    ...cardOverrides,
  };
}

function makeSessionsResponse(): TimelineSessionsListResponse {
  return {
    sessions: [makeTimelineCard()],
    total: 120,
    has_real_sessions: true,
  };
}

function makeRunner(overrides: Partial<Runner> = {}): Runner {
  const now = "2026-03-21T12:00:00Z";
  return {
    id: 1,
    owner_id: 1,
    name: "demo-machine",
    availability_policy: "always_on",
    labels: null,
    capabilities: ["exec.full"],
    status: "online",
    status_reason: null,
    status_summary: "Ready to start sessions.",
    last_seen_at: now,
    last_seen_age_seconds: 3,
    heartbeat_interval_ms: 30_000,
    stale_after_seconds: 90,
    runner_metadata: { hostname: "demo-machine" },
    install_mode: "native",
    auto_update_policy: "notify",
    install_layout_version: 1,
    managed_install_ready: true,
    runner_version: "1.0.0",
    latest_runner_version: "1.0.0",
    version_status: "current",
    reported_capabilities: ["exec.full"],
    capabilities_match: true,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

function renderSessionsPage(initialEntry = "/timeline", queryClient = createQueryClient()) {
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route
            path="/timeline"
            element={
              <>
                <SessionsPage />
                <LocationProbe />
              </>
            }
          />
          <Route
            path="/runners"
            element={
              <>
                <div>Machines</div>
                <LocationProbe />
              </>
            }
          />
          <Route path="/settings" element={<div>Settings</div>} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>
  );

  return {
    ...rendered,
    queryClient,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-probe">{location.pathname}{location.search}</div>;
}

function setDocumentVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: state === "hidden",
  });
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: state,
  });
}

describe("SessionsPage", () => {
  let latestFilters: AgentSessionFilters | undefined;
  let latestSessionOptions: { refetchInterval?: unknown } | undefined;
  let latestTimelineStreamOptions: { enabled?: boolean; skipInitialReplay?: boolean } | undefined;

  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    latestFilters = undefined;
    latestSessionOptions = undefined;
    latestTimelineStreamOptions = undefined;
    setDocumentVisibility("visible");
    window.localStorage.clear();
    vi.stubGlobal("EventSource", class {} as typeof EventSource);

    mockUseAgentSessions.mockImplementation((filters: AgentSessionFilters, options?: { refetchInterval?: unknown }) => {
      latestFilters = filters;
      latestSessionOptions = options;
      return {
        data: makeSessionsResponse(),
        isLoading: false,
        error: null,
        refetch: vi.fn(),
      };
    });

    mockUseAgentFilters.mockReturnValue({
      data: {
        projects: ["zerg", "longhouse"],
        providers: ["codex", "claude"],
        machines: ["laptop", "demo-machine"],
      },
      isLoading: false,
    });

    mockUseRunners.mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    });

    mockUseTimelineSessionStream.mockImplementation(
      (_filters: AgentSessionFilters, options?: { enabled?: boolean; skipInitialReplay?: boolean }) => {
      latestTimelineStreamOptions = options;
      }
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    setDocumentVisibility("visible");
    window.localStorage.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("hydrates timeline filters directly from the URL", async () => {
    renderSessionsPage(
      "/timeline?project=zerg&provider=codex&device_id=laptop&days_back=30&query=fix%20bug&mode=hybrid&sort=recent&hide_autonomous=false&limit=150"
    );

    await waitFor(() => {
      expect(latestFilters).toEqual({
        project: "zerg",
        provider: "codex",
        device_id: "laptop",
        days_back: 30,
        query: "fix bug",
        limit: 100,
        mode: "hybrid",
        sort: "recency",
        hide_autonomous: false,
      });
    });
  });

  it("maps environment URLs into the machine filter", async () => {
    renderSessionsPage("/timeline?environment=laptop");

    await waitFor(() => {
      expect(latestFilters).toMatchObject({
        device_id: "laptop",
      });
    });
  });

  it("defers filter option loading until the filter popover opens", async () => {
    const user = userEvent.setup();
    renderSessionsPage("/timeline");

    expect(mockUseAgentFilters).toHaveBeenLastCalledWith(14, false);

    await user.click(screen.getByRole("button", { name: "Filters" }));

    expect(mockUseAgentFilters).toHaveBeenLastCalledWith(14, true);
  });

  it("does not render a redundant timeline page heading above the toolbar", async () => {
    renderSessionsPage("/timeline");

    expect(await screen.findByPlaceholderText("Search sessions...")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Timeline" })).not.toBeInTheDocument();
  });



  it("disables timeline card hover transitions while the user is actively scrolling", async () => {
    vi.useFakeTimers();
    const appRoot = document.createElement("div");
    appRoot.id = "react-root";
    document.body.appendChild(appRoot);
    renderSessionsPage("/timeline");

    const scroller = document.querySelector(".page-shell");
    expect(scroller).not.toBeNull();
    expect(appRoot).not.toBeNull();
    expect(scroller).not.toHaveClass("page-shell--scrolling");
    expect(appRoot).not.toHaveClass("react-root--scrolling");

    fireEvent.wheel(scroller!);
    expect(scroller).toHaveClass("page-shell--scrolling");
    expect(appRoot).toHaveClass("react-root--scrolling");

    act(() => {
      vi.advanceTimersByTime(249);
    });
    expect(scroller).toHaveClass("page-shell--scrolling");
    expect(appRoot).toHaveClass("react-root--scrolling");

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(scroller).not.toHaveClass("page-shell--scrolling");
    expect(appRoot).not.toHaveClass("react-root--scrolling");

    appRoot.remove();
  });












  it("uses honest grouped-results copy in grouped query mode", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            id: "session-root",
            project: "query-root",
            summary_title: "query root",
            thread_root_session_id: "thread-1",
            thread_head_session_id: "thread-2",
          }),
          makeTimelineCard({
            id: "session-other",
            project: "query-other",
            summary_title: "query other",
            thread_root_session_id: "thread-3",
            thread_head_session_id: "thread-3",
          }),
        ],
        total: 3,
        has_real_sessions: true,
        query_grouping_mode: "grouped_results",
        query_grouping_has_more: false,
        query_grouping_source_count: 3,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage("/timeline?query=needle");

    expect(await screen.findByText("2 results")).toBeInTheDocument();
    expect(screen.getByText("Showing 2 grouped results from 3 matching sessions")).toBeInTheDocument();
    expect(screen.queryByText("Showing 2 of 3 task threads")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load More" })).not.toBeInTheDocument();
  });

  it("keeps grouped query load-more tied to raw matching-session exhaustion", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            id: "session-root",
            project: "query-root",
            thread_root_session_id: "thread-1",
            thread_head_session_id: "thread-2",
          }),
        ],
        total: 5,
        has_real_sessions: true,
        query_grouping_mode: "grouped_results",
        query_grouping_has_more: true,
        query_grouping_source_count: 2,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage("/timeline?query=needle");

    expect(await screen.findByRole("button", { name: "Load More" })).toBeInTheDocument();
  });

  it("shows import-first guidance when the timeline is empty", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [],
        total: 0,
        has_real_sessions: false,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage("/timeline");

    expect(await screen.findByText("Connect your first machine")).toBeInTheDocument();
    expect(screen.getByText(/Run one command on the machine where you use Claude Code/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "See setup steps" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Machines" })).toBeInTheDocument();
    expect(screen.getByText("longhouse connect --install")).toBeInTheDocument();
    expect(screen.getByText("longhouse ship")).toBeInTheDocument();
    expect(screen.queryByText("Welcome to Longhouse")).not.toBeInTheDocument();
  });

  it("does not show a redundant Machines button in the timeline header", async () => {
    mockUseRunners.mockReturnValue({
      data: [makeRunner()],
      isLoading: false,
      error: null,
    });

    renderSessionsPage("/timeline");

    await screen.findByPlaceholderText("Search sessions...");
    // Machines is a nav item — no redundant header button
    expect(screen.queryByTestId("timeline-runner-action")).not.toBeInTheDocument();
  });

  it("treats demo sessions as preview data instead of the primary onboarding path", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [makeTimelineCard()],
        total: 1,
        has_real_sessions: false,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage("/timeline");

    expect(await screen.findByText("These are demo sessions.")).toBeInTheDocument();
    expect(screen.getByText("longhouse connect --install")).toBeInTheDocument();
    expect(screen.getByText("longhouse ship")).toBeInTheDocument();
    expect(screen.getByText(/launch managed sessions with Longhouse when you want control after launch/i)).toBeInTheDocument();
  });


  it("refreshes relative time labels while the timeline stays open", async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-03-21T12:00:45Z"));

      mockUseAgentSessions.mockImplementation((filters: AgentSessionFilters, options?: { refetchInterval?: unknown }) => {
        latestFilters = filters;
        latestSessionOptions = options;
        return {
          data: {
            sessions: [
              makeTimelineCard({
                started_at: "2026-03-21T12:00:00Z",
                last_activity_at: "2026-03-21T12:00:00Z",
                timeline_anchor_at: "2026-03-21T12:00:00Z",
                ended_at: null,
                status: "idle",
                display_phase: "Idle",
              }),
            ],
            total: 1,
            has_real_sessions: true,
          },
          isLoading: false,
          error: null,
          refetch: vi.fn(),
        };
      });

      renderSessionsPage("/timeline");

      expect(screen.getByText("Started Just now")).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(15_000);
      });

      expect(screen.getByText("Started 1m ago")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses a slow reconciliation poll when the timeline SSE stream is active", async () => {
    renderSessionsPage("/timeline");

    await waitFor(() => {
      expect(latestSessionOptions?.refetchInterval).toBe(120000);
    });
  });

  it("waits for the initial timeline data before opening the SSE stream", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage("/timeline");

    await waitFor(() => {
      expect(latestTimelineStreamOptions?.enabled).toBe(false);
    });
  });

  it("does not re-skip timeline replay when reconnecting the same filter set", async () => {
    renderSessionsPage("/timeline");

    await waitFor(() => {
      expect(latestTimelineStreamOptions?.enabled).toBe(true);
    });

    act(() => {
      setDocumentVisibility("hidden");
      document.dispatchEvent(new Event("visibilitychange"));
    });

    await waitFor(() => {
      expect(latestTimelineStreamOptions?.enabled).toBe(false);
    });

    act(() => {
      setDocumentVisibility("visible");
      document.dispatchEvent(new Event("visibilitychange"));
    });

    await waitFor(() => {
      expect(latestTimelineStreamOptions?.enabled).toBe(true);
      expect(latestTimelineStreamOptions?.skipInitialReplay).toBe(false);
    });
  });

  it("pauses the timeline SSE stream while the page is hidden", async () => {
    setDocumentVisibility("hidden");
    renderSessionsPage("/timeline");

    await waitFor(() => {
      expect(latestTimelineStreamOptions?.enabled).toBe(false);
    });

    act(() => {
      setDocumentVisibility("visible");
      document.dispatchEvent(new Event("visibilitychange"));
    });

    await waitFor(() => {
      expect(latestTimelineStreamOptions?.enabled).toBe(true);
    });
  });

  it("falls back to normal polling when EventSource is unavailable", async () => {
    vi.stubGlobal("EventSource", undefined);
    renderSessionsPage("/timeline");

    await waitFor(() => {
      expect(latestTimelineStreamOptions?.enabled).toBe(false);
      expect(latestSessionOptions?.refetchInterval).not.toBe(120000);
      expect(typeof latestSessionOptions?.refetchInterval).toBe("function");
    });
  });

  it("resets pagination immediately and debounces the query filter", async () => {
    renderSessionsPage("/timeline?limit=150");

    const input = await screen.findByPlaceholderText("Search sessions...");
    fireEvent.change(input, { target: { value: "alpha" } });

    await waitFor(() => {
      expect(latestFilters?.limit).toBe(50);
      expect(latestFilters?.query).toBeUndefined();
    });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 350));
    });

    await waitFor(() => {
      expect(latestFilters?.query).toBe("alpha");
      expect(latestFilters?.limit).toBe(50);
    });
  });

  it("keeps pagination in the URL-owned filter contract", async () => {
    const user = userEvent.setup();
    renderSessionsPage("/timeline?project=zerg");

    const loadMoreButton = await screen.findByRole("button", { name: "Load More" });
    await user.click(loadMoreButton);

    await waitFor(() => {
      expect(latestFilters).toMatchObject({
        project: "zerg",
        limit: 100,
      });
    });
  });

  it("removes the provider chip without introducing a 1d filter", async () => {
    const user = userEvent.setup();
    renderSessionsPage("/timeline?provider=claude");

    const dismissButton = await screen.findByLabelText("Remove claude filter");
    await user.click(dismissButton);

    await waitFor(() => {
      expect(screen.queryByLabelText("Remove claude filter")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Remove 1d filter")).not.toBeInTheDocument();
      expect(screen.getByTestId("location-probe")).toHaveTextContent("/timeline");
    });
  });

  it("treats a blank days_back param as the default window", async () => {
    renderSessionsPage("/timeline?provider=claude&days_back=");

    await waitFor(() => {
      expect(latestFilters?.days_back).toBe(14);
      expect(screen.queryByLabelText("Remove 1d filter")).not.toBeInTheDocument();
    });
  });




  it("uses generated title with first prompt subheading and ignores summary or live transcript card copy", async () => {
    const receivedAt = new Date(Date.now() - 45_000).toISOString();
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            summary: "Older generated summary.",
            summary_title: "Generated subject",
            first_user_message: "Original user prompt for this session.",
            capabilities: makeCapabilities({
              live_control_available: true,
              host_reattach_available: true,
              reply_to_live_session_available: true,
            }),
            control: {
              source_runner_id: 7,
              source_runner_name: "cinder",
              attach_command: "longhouse-engine codex-bridge attach --session-id session-1",
            },
            transcript_preview: {
              event_id: 101,
              text: "The provider already streamed this answer before the durable transcript poll landed.",
              event_origin: "live_provisional",
              timestamp: receivedAt,
              is_complete: false,
              content_cursor: "codex_bridge_live:session-1:thread-1:turn-1:12",
              is_provisional: true,
              is_stale: false,
              stale_reason: null,
            },
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage();

    expect(await screen.findByText("Generated subject")).toBeInTheDocument();
    expect(await screen.findByText("Original user prompt for this session.")).toBeInTheDocument();
    expect(screen.queryByText("Older generated summary.")).not.toBeInTheDocument();
    expect(screen.queryByText("The provider already streamed this answer")).not.toBeInTheDocument();
    expect(screen.queryByTestId("session-card-transcript-preview")).not.toBeInTheDocument();
  });

  it("does not let stale partial transcript preview or summary replace first prompt card copy", async () => {
    const staleReceivedAt = new Date(Date.now() - 45_000).toISOString();
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            summary: "Current durable summary.",
            first_user_message: "Stable first prompt.",
            transcript_preview: {
              event_id: 102,
              text: "Partial text from a bridge that stopped sending updates.",
              event_origin: "live_provisional",
              timestamp: staleReceivedAt,
              is_complete: false,
              content_cursor: "codex_bridge_live:session-1:thread-1:turn-1:3",
              is_provisional: true,
              is_stale: true,
              stale_reason: "freshness_window_expired",
            },
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage();

    expect(await screen.findByText("Stable first prompt.")).toBeInTheDocument();
    expect(screen.queryByText("Current durable summary.")).not.toBeInTheDocument();
    expect(screen.queryByTestId("session-card-transcript-preview")).not.toBeInTheDocument();
  });

  it("keeps first prompt card copy even when the server has a fresh transcript preview", async () => {
    const oldButServerCurrent = new Date(Date.now() - 5 * 60_000).toISOString();
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            summary: "Durable summary should stay behind the current server preview.",
            first_user_message: "Stable prompt beats live preview.",
            transcript_preview: {
              event_id: 103,
              text: "Server says this complete bridge snapshot is still the card preview.",
              event_origin: "live_provisional",
              timestamp: oldButServerCurrent,
              is_complete: true,
              content_cursor: "codex_bridge_live:session-1:thread-1:turn-1:10",
              is_provisional: true,
              is_stale: false,
              stale_reason: null,
            },
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage();

    expect(await screen.findByText("Stable prompt beats live preview.")).toBeInTheDocument();
    expect(screen.queryByText("Server says this complete bridge snapshot")).not.toBeInTheDocument();
    expect(screen.queryByText("Durable summary should stay behind the current server preview.")).not.toBeInTheDocument();
  });

  it("uses the first user message instead of a generating-summary placeholder", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            summary: null,
            summary_title: null,
            first_user_message: "Workshop an inbox-style homepage layout for Longhouse timeline cards.",
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage();

    expect(await screen.findAllByText("Workshop an inbox-style homepage layout for Longhouse timeline cards.")).toHaveLength(1);
    expect(screen.queryByText(/Generating summary/)).not.toBeInTheDocument();
  });

  it("uses deterministic copy before any transcript arrives", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            provider: "claude",
            project: "zerg",
            summary: null,
            summary_title: null,
            first_user_message: null,
            user_messages: 0,
            assistant_messages: 0,
            tool_calls: 0,
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage();

    expect(await screen.findByText("New Claude session in zerg")).toBeInTheDocument();
    expect(screen.queryByText(/Generating summary/)).not.toBeInTheDocument();
  });






  it("does not treat a merely open session as live without runtime evidence", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            status: undefined,
            confidence: undefined,
            display_phase: undefined,
            last_live_at: undefined,
            presence_state: undefined,
            presence_tool: undefined,
            presence_updated_at: undefined,
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage();

    expect(await screen.findByText("clean this up")).toBeInTheDocument();
    expect(screen.queryByText("Working")).not.toBeInTheDocument();
    expect(screen.queryByText("Fresh signal")).not.toBeInTheDocument();
  });


  it("uses the session start time for the card timestamp", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-03-21T12:05:00Z"));

    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            started_at: "2026-03-21T11:00:00Z",
            ended_at: "2026-03-21T12:00:00Z",
            last_activity_at: "2026-03-21T12:00:00Z",
            timeline_anchor_at: "2026-03-21T12:03:00Z",
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage();

    expect(screen.getByText("Started 1h ago")).toBeInTheDocument();
  });





});
