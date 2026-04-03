import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes, useLocation } from "react-router-dom";
import * as reactRouterDom from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as agentsApi from "../../services/api/agents";
import type {
  AgentSession,
  AgentSessionFilters,
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

function makeSession(overrides: Partial<AgentSession> = {}): AgentSession {
  const now = "2026-03-21T12:00:00Z";
  return {
    id: "session-1",
    provider: "codex",
    project: "zerg",
    device_id: "device-1",
    environment: "laptop",
    cwd: "/Users/davidrose/git/zerg",
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
    execution_home: "legacy",
    branched_from_event_id: null,
    is_writable_head: true,
    loop_mode: "manual",
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
    name: "cube",
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
    runner_metadata: { hostname: "cube" },
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
          <Route path="/briefings" element={<div>Briefings</div>} />
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
        machines: ["laptop", "cube"],
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
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("hydrates timeline filters directly from the URL", async () => {
    renderSessionsPage(
      "/timeline?project=zerg&provider=codex&environment=laptop&days_back=30&query=fix%20bug&mode=hybrid&sort=recent&hide_autonomous=false&limit=150"
    );

    await waitFor(() => {
      expect(latestFilters).toEqual({
        project: "zerg",
        provider: "codex",
        environment: "laptop",
        days_back: 30,
        query: "fix bug",
        limit: 150,
        mode: "hybrid",
        sort: "recency",
        hide_autonomous: false,
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

  it("prefetches the session workspace queries when a session card gets hover intent", async () => {
    const queryClient = createQueryClient();
    const prefetchSpy = vi.spyOn(queryClient, "prefetchQuery").mockImplementation(async (options) => {
      await options.queryFn?.();
    });
    const workspaceSpy = vi.spyOn(agentsApi, "fetchAgentSessionWorkspace").mockResolvedValue({
      session: makeSession(),
      thread: {
        root_session_id: "session-1",
        head_session_id: "session-1",
        sessions: [makeSession()],
      },
      projection: {
        root_session_id: "session-1",
        focus_session_id: "session-1",
        head_session_id: "session-1",
        path_session_ids: ["session-1"],
        items: [],
        total: 0,
      },
    });

    renderSessionsPage("/timeline", queryClient);

    fireEvent.mouseEnter(await screen.findByTestId("session-card"));

    await waitFor(() => {
      expect(prefetchSpy).toHaveBeenCalledTimes(1);
      expect(prefetchSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          queryKey: ["agent-session-workspace", "session-1", { limit: 200, branch_mode: "head" }],
          staleTime: 10_000,
        }),
      );
      expect(workspaceSpy).toHaveBeenCalledWith("session-1", {
        limit: 200,
        branch_mode: "head",
      });
    });
  });

  it("does not start a workspace prefetch on mouse pointer-down", async () => {
    const queryClient = createQueryClient();
    const prefetchSpy = vi.spyOn(queryClient, "prefetchQuery").mockImplementation(async (options) => {
      await options.queryFn?.();
    });

    renderSessionsPage("/timeline", queryClient);

    fireEvent.pointerDown(await screen.findByTestId("session-card"), { pointerType: "mouse" });

    expect(prefetchSpy).not.toHaveBeenCalled();
  });

  it("uses honest grouped-results copy in query compatibility mode", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            id: "session-root",
            project: "compat",
            summary_title: "compat root",
            thread_root_session_id: "thread-1",
            thread_head_session_id: "thread-2",
          }),
          makeTimelineCard({
            id: "session-other",
            project: "compat-2",
            summary_title: "compat other",
            thread_root_session_id: "thread-3",
            thread_head_session_id: "thread-3",
          }),
        ],
        total: 3,
        has_real_sessions: true,
        compatibility_mode: "query_grouped",
        compatibility_has_more: false,
        compatibility_source_count: 3,
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

  it("keeps query compatibility load-more tied to raw matching-session exhaustion", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            id: "session-root",
            project: "compat",
            thread_root_session_id: "thread-1",
            thread_head_session_id: "thread-2",
          }),
        ],
        total: 5,
        has_real_sessions: true,
        compatibility_mode: "query_grouped",
        compatibility_has_more: true,
        compatibility_source_count: 2,
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

    expect(await screen.findByText("Import sessions you already have")).toBeInTheDocument();
    expect(screen.getByText(/Longhouse gets useful once it can see real Claude Code, Codex, or Gemini work/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "See import steps" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect Machine" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load demo sessions instead" })).toBeInTheDocument();
    expect(screen.getByText("longhouse connect --install")).toBeInTheDocument();
    expect(screen.getByText("longhouse ship")).toBeInTheDocument();
    expect(screen.getByText("longhouse claude")).toBeInTheDocument();
    expect(screen.queryByText("Welcome to Longhouse")).not.toBeInTheDocument();
  });

  it("opens the launch modal directly from the timeline when exactly one runner is ready", async () => {
    const user = userEvent.setup();

    mockUseRunners.mockReturnValue({
      data: [makeRunner()],
      isLoading: false,
      error: null,
    });

    renderSessionsPage("/timeline");

    await user.click(await screen.findByRole("button", { name: "Start Session" }));

    expect(screen.getByTestId("launch-session-modal")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Start session" })).toBeInTheDocument();
    expect(screen.getByText(/keep the same session available from the timeline later/i)).toBeInTheDocument();
  });

  it("sends users to runners when they need to choose a launch target", async () => {
    const user = userEvent.setup();
    const navigateMock = vi.fn();
    vi.spyOn(reactRouterDom, "useNavigate").mockReturnValue(navigateMock);

    mockUseRunners.mockReturnValue({
      data: [
        makeRunner({ id: 1, name: "cube" }),
        makeRunner({ id: 2, name: "mac-mini" }),
      ],
      isLoading: false,
      error: null,
    });

    renderSessionsPage("/timeline");

    await user.click(await screen.findByRole("button", { name: "Choose Machine" }));

    expect(navigateMock).toHaveBeenCalledWith("/runners");
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
    expect(screen.getByText(/start through longhouse or wrappers when you want control after launch/i)).toBeInTheDocument();
  });

  it("renders query compatibility cards from the matched detail session instead of speculative head state", async () => {
    const detail = makeSession({
      id: "matched-continuation",
      project: "search-hit-project",
      summary_title: "Matched continuation",
      summary: "Older matched continuation summary",
      started_at: "2026-03-20T12:00:00Z",
      last_activity_at: "2026-03-20T12:30:00Z",
      match_event_id: 42,
      match_snippet: "needle in the older continuation",
      thread_root_session_id: "thread-1",
      thread_head_session_id: "head-session",
      thread_continuation_count: 3,
      execution_home: "legacy",
      status: "completed",
      display_phase: "Completed",
    });
    const head = makeSession({
      id: "head-session",
      project: "current-head-project",
      summary_title: "Current writable head",
      summary: "Newest head summary",
      started_at: "2026-03-21T12:00:00Z",
      last_activity_at: "2026-03-21T12:45:00Z",
      thread_root_session_id: "thread-1",
      thread_head_session_id: "head-session",
      thread_continuation_count: 3,
      execution_home: "managed_local",
      status: "working",
      presence_state: "running",
      display_phase: "Running bash",
      active_tool: "bash",
    });

    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard(detail, {
            thread_id: "thread-1",
            head,
            detail,
            root: makeSession({
              id: "root-session",
              project: "root-project",
              thread_root_session_id: "thread-1",
              thread_head_session_id: "head-session",
            }),
            continuation_count: 3,
            started_origin_label: "On this Mac",
            head_origin_label: "Cloud",
          }),
        ],
        total: 1,
        has_real_sessions: true,
        compatibility_mode: "query_grouped",
        compatibility_has_more: false,
        compatibility_source_count: 1,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage("/timeline?query=needle");

    expect(await screen.findByText("Matched continuation")).toBeInTheDocument();
    expect(screen.queryByText("Current writable head")).not.toBeInTheDocument();
    expect(screen.queryByText("Running bash")).not.toBeInTheDocument();
    expect(screen.queryByText(/^Head:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Started:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/continuations/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open match: Matched continuation" })).toBeInTheDocument();
    expect(screen.getByText(/^Matched .*ago$/)).toBeInTheDocument();
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

      expect(screen.getByText("Just now")).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(15_000);
      });

      expect(screen.getByText("1m ago")).toBeInTheDocument();
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

  it("renders live runtime state directly on the main timeline cards", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: "2026-03-21T12:03:00Z",
            status: "working",
            presence_state: "running",
            presence_tool: "bash",
            presence_updated_at: "2026-03-21T12:04:00Z",
            last_live_at: "2026-03-21T12:04:00Z",
            display_phase: "Running bash",
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

    expect(await screen.findByText("Running bash")).toBeInTheDocument();
    expect(screen.queryByText("Fresh signal")).not.toBeInTheDocument();
    expect(screen.queryByText("In progress")).not.toBeInTheDocument();
  });

  it("shows execution-home badges directly on the main timeline cards", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            execution_home: "managed_local",
            origin_label: "cinder",
          }),
          makeTimelineCard({
            id: "session-2",
            project: "cloud",
            summary_title: "Cloud branch",
            execution_home: "cloud_takeover",
            origin_label: "Cloud",
            thread_root_session_id: "session-2",
            thread_head_session_id: "session-2",
          }),
        ],
        total: 2,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage();

    expect(await screen.findByText("Live control")).toBeInTheDocument();
    expect(screen.getByText("Cloud")).toBeInTheDocument();
    expect(screen.getByText("Head: cinder")).toBeInTheDocument();
    expect(screen.queryByText("Head: Cloud")).not.toBeInTheDocument();
  });

  it("marks recent-progress sessions without semantic live signals honestly", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            status: "active",
            confidence: "inferred",
            display_phase: "Recent progress",
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

    expect(await screen.findByText("Recent progress")).toBeInTheDocument();
    expect(screen.queryByText("In progress")).not.toBeInTheDocument();
  });

  it("does not style inferred recent progress as currently executing", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            status: "active",
            confidence: "inferred",
            display_phase: "Recent progress",
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    const { container } = renderSessionsPage();

    await screen.findByText("Recent progress");

    const card = container.querySelector(".session-card");
    expect(card).toHaveClass("session-card--inferred");
    expect(card).not.toHaveClass("session-card--live");
    expect(card).not.toHaveClass("session-card--running");
    expect(card).not.toHaveClass("session-card--thinking");
  });

  it("styles needs-you sessions as attention state, not executing work", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            execution_home: "managed_local",
            managed_transport: "tmux",
            runtime_source: "managed_local_transport",
            status: "active",
            confidence: "live",
            presence_state: "needs_user",
            presence_updated_at: "2026-03-21T12:04:00Z",
            last_live_at: "2026-03-21T12:04:00Z",
            timeline_anchor_at: "2026-03-21T12:04:00Z",
            display_phase: "Needs you",
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    const { container } = renderSessionsPage();

    expect(await screen.findByText("Needs you")).toBeInTheDocument();
    expect(screen.getByText("Live on host")).toBeInTheDocument();

    const card = container.querySelector(".session-card");
    expect(card).toHaveClass("session-card--needs-user");
    expect(card).not.toHaveClass("session-card--live");
    expect(card).not.toHaveClass("session-card--running");
    expect(card).not.toHaveClass("session-card--thinking");
  });

  it("styles blocked sessions as attention state, not executing work", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            execution_home: "managed_local",
            managed_transport: "tmux",
            runtime_source: "managed_local_transport",
            status: "active",
            confidence: "live",
            presence_state: "blocked",
            presence_tool: "bash",
            presence_updated_at: "2026-03-21T12:05:00Z",
            last_live_at: "2026-03-21T12:05:00Z",
            timeline_anchor_at: "2026-03-21T12:05:00Z",
            display_phase: "Blocked on bash",
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    const { container } = renderSessionsPage();

    expect(await screen.findByText("Blocked on bash")).toBeInTheDocument();
    expect(screen.getByText("Live on host")).toBeInTheDocument();

    const card = container.querySelector(".session-card");
    expect(card).toHaveClass("session-card--blocked");
    expect(card).not.toHaveClass("session-card--live");
    expect(card).not.toHaveClass("session-card--running");
    expect(card).not.toHaveClass("session-card--thinking");
  });

  it("styles thinking sessions as active execution distinct from running", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            status: "working",
            confidence: "live",
            presence_state: "thinking",
            presence_updated_at: "2026-03-21T12:04:00Z",
            last_live_at: "2026-03-21T12:04:00Z",
            timeline_anchor_at: "2026-03-21T12:04:00Z",
            display_phase: "Thinking",
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    const { container } = renderSessionsPage();

    expect(await screen.findByText("Thinking")).toBeInTheDocument();
    expect(screen.queryByText("Fresh signal")).not.toBeInTheDocument();

    const card = container.querySelector(".session-card");
    expect(card).toHaveClass("session-card--live");
    expect(card).toHaveClass("session-card--thinking");
    expect(card).not.toHaveClass("session-card--running");
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

    expect(await screen.findByText("Cleanup sessions page")).toBeInTheDocument();
    expect(screen.queryByText("Working")).not.toBeInTheDocument();
    expect(screen.queryByText("Recent progress")).not.toBeInTheDocument();
    expect(screen.queryByText("Fresh signal")).not.toBeInTheDocument();
  });

  it("uses the session timeline anchor for the card timestamp", async () => {
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

    expect(screen.getByText("2m ago")).toBeInTheDocument();
  });

  it("preserves backend thread-card ordering without regrouping client-side", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            id: "session-beta",
            project: "beta",
            summary_title: "beta",
            started_at: "2026-03-21T12:00:00Z",
            last_activity_at: "2026-03-21T12:00:00Z",
            thread_root_session_id: "session-beta",
            thread_head_session_id: "session-beta",
          }),
          makeTimelineCard({
            id: "session-alpha",
            project: "alpha",
            summary_title: "alpha",
            started_at: "2026-03-20T12:00:00Z",
            last_activity_at: "2026-03-20T12:00:00Z",
            timeline_anchor_at: "2026-03-21T12:04:00Z",
            thread_root_session_id: "session-alpha",
            thread_head_session_id: "session-alpha",
          }),
        ],
        total: 2,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    const { container } = renderSessionsPage();

    const projects = Array.from(container.querySelectorAll(".session-card-project")).map((node) => node.textContent);
    expect(projects[0]).toBe("beta");
    expect(projects[1]).toBe("alpha");
  });

});
