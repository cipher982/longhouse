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
  SessionCapabilities,
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
    home_label: null,
    branched_from_event_id: null,
    is_writable_head: true,
    control: null,
    capabilities: makeCapabilities(),
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

  it("maps legacy environment URLs into the machine filter", async () => {
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

    const card = await screen.findByTestId("session-card");

    fireEvent.mouseEnter(card);
    expect(prefetchSpy).not.toHaveBeenCalled();

    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 220));
    });

    expect(prefetchSpy).toHaveBeenCalledTimes(1);
    expect(prefetchSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        queryKey: ["agent-session-workspace", "session-1", { limit: 200, branch_mode: "head" }],
        staleTime: 10_000,
      }),
    );
    await waitFor(() => {
      expect(workspaceSpy).toHaveBeenCalledWith("session-1", {
        limit: 200,
        branch_mode: "head",
      });
    });
  });

  it("suppresses hover prefetch while the timeline is actively scrolling", async () => {
    const queryClient = createQueryClient();
    const prefetchSpy = vi.spyOn(queryClient, "prefetchQuery").mockImplementation(async (options) => {
      await options.queryFn?.();
    });

    renderSessionsPage("/timeline", queryClient);

    const card = await screen.findByTestId("session-card");
    const scroller = document.querySelector(".page-shell");
    expect(scroller).not.toBeNull();

    vi.useFakeTimers();
    try {
      fireEvent.scroll(scroller!);
      fireEvent.mouseEnter(card);

      act(() => {
        vi.advanceTimersByTime(220);
      });

      expect(prefetchSpy).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
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

  it("does not start a workspace prefetch on mouse pointer-down", async () => {
    const queryClient = createQueryClient();
    const prefetchSpy = vi.spyOn(queryClient, "prefetchQuery").mockImplementation(async (options) => {
      await options.queryFn?.();
    });

    renderSessionsPage("/timeline", queryClient);

    fireEvent.pointerDown(await screen.findByTestId("session-card"), { pointerType: "mouse" });

    expect(prefetchSpy).not.toHaveBeenCalled();
  });

  it("keeps imported session cards free of always-on management chrome", async () => {
    renderSessionsPage("/timeline");

    expect(await screen.findByText("Cleanup sessions page")).toBeInTheDocument();
    expect(screen.queryByText("Unmanaged")).not.toBeInTheDocument();
    expect(screen.queryByTestId("session-card-management")).not.toBeInTheDocument();
    expect(screen.queryByTestId("session-card-capability")).not.toBeInTheDocument();
  });

  it("marks closed imported sessions as closed without provider-colored card styling", async () => {
    renderSessionsPage("/timeline");

    const card = await screen.findByTestId("session-card");
    expect(card).toHaveAttribute("data-card-state", "closed");
    expect(card).toHaveClass("session-card--closed");
    expect(card.style.borderLeftColor).toBe("");
    expect(screen.getByTestId("session-card-closed-state")).toHaveTextContent("Closed");
  });

  it("treats stale status-only activity on an ended import as closed", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: "2026-03-21T12:10:00Z",
            status: "active",
            confidence: "inferred",
            runtime_source: "progress",
            presence_state: null,
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage("/timeline");

    const card = await screen.findByTestId("session-card");
    expect(card).toHaveAttribute("data-card-state", "closed");
    expect(card).toHaveClass("session-card--closed");
  });

  it("treats stale presence on an ended import as closed", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: "2026-03-21T12:10:00Z",
            status: "idle",
            confidence: "live",
            runtime_source: "semantic",
            presence_state: "idle",
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage("/timeline");

    const card = await screen.findByTestId("session-card");
    expect(card).toHaveAttribute("data-card-state", "closed");
    expect(card).toHaveClass("session-card--closed");
  });

  it("shows a control-offline badge when a managed session cannot accept browser prompts", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            provider: "codex",
            control: {
              managed_transport: "codex_app_server",
              source_runner_id: null,
              source_runner_name: null,
              attach_command: "longhouse codex --attach",
            },
            capabilities: makeCapabilities({
              host_reattach_available: true,
              display_label: "Reconnect required",
              display_tone: "warning",
            }),
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage("/timeline");

    const card = await screen.findByTestId("session-card");
    expect(card).toHaveAttribute("data-card-state", "actionable");
    expect(card).not.toHaveClass("session-card--closed");
    expect(screen.queryByTestId("session-card-closed-state")).not.toBeInTheDocument();

    const capability = await screen.findByTestId("session-card-capability");
    expect(capability).toHaveTextContent("Reconnect required");
    expect(capability).toHaveClass("session-card-capability-pill--warning");
    expect(capability).toHaveAttribute(
      "title",
      "Longhouse can see this live Codex session, but cannot send prompts until the engine reconnects.",
    );
    expect(screen.queryByTestId("session-card-management")).not.toBeInTheDocument();
  });

  it("treats ended managed sessions with stale reattach metadata as closed", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: "2026-03-21T12:10:00Z",
            runtime_source: "managed_local_transport",
            status: "idle",
            confidence: "live",
            presence_state: null,
            control: {
              managed_transport: "codex_app_server",
              source_runner_id: null,
              source_runner_name: null,
              attach_command: "longhouse codex --attach",
            },
            capabilities: makeCapabilities({
              host_reattach_available: true,
            }),
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage("/timeline");

    const card = await screen.findByTestId("session-card");
    expect(card).toHaveAttribute("data-card-state", "closed");
    expect(card).toHaveClass("session-card--closed");
    expect(screen.getByTestId("session-card-closed-state")).toHaveTextContent("Closed");
  });

  it("keeps ended sessions with current controlled presence actionable", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: "2026-03-21T12:10:00Z",
            runtime_source: "semantic",
            status: "idle",
            confidence: "live",
            presence_state: "idle",
            presence_updated_at: "2026-03-21T12:11:00Z",
            last_live_at: "2026-03-21T12:11:00Z",
            control: {
              managed_transport: "codex_app_server",
              source_runner_id: "runner-1",
              source_runner_name: "Laptop",
              attach_command: "longhouse codex --attach",
            },
            capabilities: makeCapabilities({
              live_control_available: true,
              host_reattach_available: true,
              reply_to_live_session_available: true,
            }),
          }),
        ],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage("/timeline");

    const card = await screen.findByTestId("session-card");
    expect(card).toHaveAttribute("data-card-state", "actionable");
    expect(card).not.toHaveClass("session-card--closed");
    expect(screen.queryByTestId("session-card-closed-state")).not.toBeInTheDocument();
  });

  it("keeps the timeline card action semantically honest", async () => {
    renderSessionsPage("/timeline");

    expect(await screen.findByRole("button", { name: "Open session: Cleanup sessions page" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue here: Cleanup sessions page" })).not.toBeInTheDocument();
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

  it("renders query compatibility cards from the matched detail session instead of speculative head state", async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-03-21T12:45:00Z"));

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
        home_label: null,
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
        home_label: "On this Mac",
        status: "working",
        presence_state: "running",
        display_phase: "Running bash",
        active_tool: "bash",
        capabilities: makeCapabilities({
          live_control_available: true,
          host_reattach_available: true,
          reply_to_live_session_available: true,
        }),
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

      expect(screen.getByText("Matched continuation")).toBeInTheDocument();
      expect(screen.queryByText("Current writable head")).not.toBeInTheDocument();
      expect(screen.queryByText("Running bash")).not.toBeInTheDocument();
      expect(screen.queryByText(/^Head:/)).not.toBeInTheDocument();
      expect(screen.queryByText(/^Started:/)).not.toBeInTheDocument();
      expect(screen.queryByText(/continuations/)).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Open match: Matched continuation" })).toBeInTheDocument();
      expect(screen.getByText(/^Matched .*ago$/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
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

  it("renders outcome runtime state directly on unmanaged timeline cards", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
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

    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(screen.queryByText("Running Shell")).not.toBeInTheDocument();
    expect(screen.queryByText("Working")).not.toBeInTheDocument();
    expect(screen.queryByText("Fresh signal")).not.toBeInTheDocument();
    expect(screen.queryByText("In progress")).not.toBeInTheDocument();
  });

  it("hides origin badges on main timeline cards and keeps continuations quiet", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            home_label: "On this Mac",
            origin_label: "cinder",
            thread_continuation_count: 3,
            capabilities: makeCapabilities({
              live_control_available: true,
              host_reattach_available: true,
              reply_to_live_session_available: true,
            }),
          }),
          makeTimelineCard({
            id: "session-2",
            project: "cloud",
            summary_title: "Cloud branch",
            home_label: "Moved to cloud",
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

    expect(screen.queryByTestId("session-card-management")).not.toBeInTheDocument();
    expect(screen.queryByTestId("session-card-capability")).not.toBeInTheDocument();
    expect(screen.queryByText("This machine")).not.toBeInTheDocument();
    expect(screen.queryByText("Head: cinder")).not.toBeInTheDocument();
    expect(screen.queryByText("Head: Cloud")).not.toBeInTheDocument();
    expect(screen.getByText(/Started .+ \u2022 3 continuations/)).toBeInTheDocument();
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

    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(screen.queryByText("Recent progress")).not.toBeInTheDocument();
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

    await screen.findByText("Active");

    const card = container.querySelector(".session-card");
    expect(card).toHaveClass("session-card--inferred");
    expect(card).toHaveAttribute("data-card-state", "actionable");
    expect(card).not.toHaveClass("session-card--closed");
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
            runtime_source: "managed_local_transport",
            status: "active",
            confidence: "live",
            presence_state: "needs_user",
            presence_updated_at: "2026-03-21T12:04:00Z",
            last_live_at: "2026-03-21T12:04:00Z",
            timeline_anchor_at: "2026-03-21T12:04:00Z",
            display_phase: "Needs you",
            capabilities: makeCapabilities({
              live_control_available: true,
              host_reattach_available: true,
              reply_to_live_session_available: true,
            }),
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

    expect(await screen.findByText("Waiting for you")).toBeInTheDocument();
    expect(screen.getByText(/Reply needed/)).toBeInTheDocument();
    expect(screen.getByText(/Live on laptop/)).toBeInTheDocument();

    const card = container.querySelector(".session-card");
    expect(card).toHaveClass("session-card--needs-user");
    expect(card).toHaveAttribute("data-card-state", "actionable");
    expect(card).not.toHaveClass("session-card--closed");
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
            runtime_source: "managed_local_transport",
            status: "active",
            confidence: "live",
            presence_state: "blocked",
            presence_tool: "bash",
            presence_updated_at: "2026-03-21T12:05:00Z",
            last_live_at: "2026-03-21T12:05:00Z",
            timeline_anchor_at: "2026-03-21T12:05:00Z",
            display_phase: "Blocked on bash",
            capabilities: makeCapabilities({
              live_control_available: true,
              host_reattach_available: true,
              reply_to_live_session_available: true,
            }),
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

    expect(await screen.findByText("Waiting for you")).toBeInTheDocument();
    expect(screen.getByText(/Approval needed .* Shell/)).toBeInTheDocument();
    expect(screen.getByText(/Live on laptop/)).toBeInTheDocument();

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
            runtime_source: "managed_local_transport",
            presence_state: "thinking",
            presence_updated_at: "2026-03-21T12:04:00Z",
            last_live_at: "2026-03-21T12:04:00Z",
            timeline_anchor_at: "2026-03-21T12:04:00Z",
            display_phase: "Thinking",
            capabilities: makeCapabilities({
              live_control_available: true,
              host_reattach_available: true,
            }),
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

    expect(await screen.findByText("Working")).toBeInTheDocument();
    expect(await screen.findByText(/Thinking/)).toBeInTheDocument();
    expect(screen.getByText(/Live on laptop/)).toBeInTheDocument();
    expect(screen.queryByText("Fresh signal")).not.toBeInTheDocument();

    const card = container.querySelector(".session-card");
    expect(card).toHaveClass("session-card--live");
    expect(card).toHaveClass("session-card--thinking");
    expect(card).not.toHaveClass("session-card--running");
  });

  it("shows managed-local inferred progress as working instead of ready", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            status: "active",
            confidence: "inferred",
            runtime_source: "semantic",
            presence_state: null,
            last_live_at: "2026-03-21T12:04:00Z",
            last_activity_at: "2026-03-21T12:04:00Z",
            timeline_anchor_at: "2026-03-21T12:04:00Z",
            display_phase: "Recent progress",
            capabilities: makeCapabilities({
              live_control_available: true,
              host_reattach_available: true,
            }),
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

    expect(await screen.findByText("Working")).toBeInTheDocument();
    expect(screen.getByText(/Recent progress .* Live on laptop/)).toBeInTheDocument();
    expect(screen.queryByText("Ready")).not.toBeInTheDocument();
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

  it("condenses raw runtime tool ids into readable card labels", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeTimelineCard({
            ended_at: null,
            status: "working",
            confidence: "live",
            runtime_source: "managed_local_transport",
            presence_state: "running",
            presence_tool: "mcp__hatch__hatch_codex",
            active_tool: "mcp__hatch__hatch_codex",
            presence_updated_at: "2026-03-21T12:04:00Z",
            last_live_at: "2026-03-21T12:04:00Z",
            timeline_anchor_at: "2026-03-21T12:04:00Z",
            display_phase: "Running mcp__hatch__hatch_codex",
            capabilities: makeCapabilities({
              host_reattach_available: true,
            }),
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

    expect(await screen.findByText("Working")).toBeInTheDocument();
    expect(screen.getByText(/Running Codex/)).toBeInTheDocument();
    expect(screen.queryByText("Running mcp__hatch__hatch_codex")).not.toBeInTheDocument();
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
