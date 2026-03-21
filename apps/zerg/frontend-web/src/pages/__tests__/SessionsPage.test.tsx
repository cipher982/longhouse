import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentSession, AgentSessionFilters, AgentSessionsListResponse } from "../../services/api/agents";
import type { ActiveSession } from "../../hooks/useActiveSessions";
import { TestRouter } from "../../test/test-utils";
import SessionsPage from "../SessionsPage";

const hookMocks = vi.hoisted(() => ({
  useAgentSessions: vi.fn(),
  useAgentFilters: vi.fn(),
}));

const activeSessionMocks = vi.hoisted(() => ({
  useActiveSessions: vi.fn(),
}));

const timelineStreamMocks = vi.hoisted(() => ({
  useTimelineSessionStream: vi.fn(),
}));

vi.mock("../../hooks/useAgentSessions", () => ({
  useAgentSessions: hookMocks.useAgentSessions,
  useAgentFilters: hookMocks.useAgentFilters,
}));

vi.mock("../../hooks/useActiveSessions", () => ({
  useActiveSessions: activeSessionMocks.useActiveSessions,
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
const { useActiveSessions: mockUseActiveSessions } = activeSessionMocks;
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
    branched_from_event_id: null,
    is_writable_head: true,
    loop_mode: "manual",
    ...overrides,
  };
}

function makeSessionsResponse(): AgentSessionsListResponse {
  return {
    sessions: [makeSession()],
    total: 120,
    has_real_sessions: true,
  };
}

function makeActiveSession(overrides: Partial<ActiveSession> = {}): ActiveSession {
  return {
    id: "session-1",
    project: "zerg",
    provider: "codex",
    cwd: "/Users/davidrose/git/zerg",
    git_repo: "https://github.com/cipher982/longhouse.git",
    git_branch: "main",
    started_at: "2026-03-21T12:00:00Z",
    ended_at: null,
    last_activity_at: "2026-03-21T12:04:00Z",
    status: "working",
    attention: "soft",
    duration_minutes: 4,
    last_user_message: "clean this up",
    last_assistant_message: "Running tests now",
    message_count: 8,
    tool_calls: 2,
    presence_state: "running",
    presence_tool: "bash",
    presence_updated_at: "2026-03-21T12:04:00Z",
    user_state: "active",
    loop_mode: "manual",
    ...overrides,
  };
}

function renderSessionsPage(initialEntry = "/timeline") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
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
          <Route path="/settings" element={<div>Settings</div>} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>
  );
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
  let latestTimelineStreamOptions: { enabled?: boolean } | undefined;

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

    mockUseActiveSessions.mockReturnValue({
      data: {
        sessions: [],
        total: 0,
        last_refresh: "2026-03-21T12:00:00Z",
      },
      isLoading: false,
      error: null,
    });

    mockUseTimelineSessionStream.mockImplementation((_filters: AgentSessionFilters, options?: { enabled?: boolean }) => {
      latestTimelineStreamOptions = options;
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    setDocumentVisibility("visible");
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

  it("uses a slow reconciliation poll when the timeline SSE stream is active", async () => {
    renderSessionsPage("/timeline");

    await waitFor(() => {
      expect(latestSessionOptions?.refetchInterval).toBe(120000);
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
          makeSession({
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
    expect(screen.getByText("Live now")).toBeInTheDocument();
    expect(screen.queryByText("In progress")).not.toBeInTheDocument();
    expect(mockUseActiveSessions).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: false,
        limit: 50,
      }),
    );
  });

  it("only enables the active sessions poll when live view is open", async () => {
    const user = userEvent.setup();
    renderSessionsPage();

    expect(mockUseActiveSessions).toHaveBeenLastCalledWith(
      expect.objectContaining({
        enabled: false,
      }),
    );

    await user.click(await screen.findByRole("button", { name: "Live view" }));

    await waitFor(() => {
      expect(mockUseActiveSessions).toHaveBeenLastCalledWith(
        expect.objectContaining({
          enabled: true,
        }),
      );
    });
  });

  it("marks open sessions as inferred when live overlay is unavailable", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [makeSession({ ended_at: null })],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderSessionsPage();

    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(screen.getByText("Inferred")).toBeInTheDocument();
    expect(screen.queryByText("In progress")).not.toBeInTheDocument();
  });

  it("uses the session timeline anchor for the card timestamp", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-03-21T12:05:00Z"));

    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeSession({
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

  it("reorders timeline cards when the session timeline anchor makes an older session newest", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeSession({
            id: "session-beta",
            project: "beta",
            summary_title: "beta",
            started_at: "2026-03-21T12:00:00Z",
            last_activity_at: "2026-03-21T12:00:00Z",
            thread_root_session_id: "session-beta",
            thread_head_session_id: "session-beta",
          }),
          makeSession({
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
    expect(projects[0]).toBe("alpha");
    expect(projects[1]).toBe("beta");
  });

  it("prefers fresher timeline runtime over an older active poll snapshot", async () => {
    const user = userEvent.setup();
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [
          makeSession({
            ended_at: null,
            status: "active",
            presence_state: "needs_user",
            presence_updated_at: "2026-03-21T12:05:00Z",
            last_live_at: "2026-03-21T12:05:00Z",
            timeline_anchor_at: "2026-03-21T12:05:00Z",
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

    mockUseActiveSessions.mockReturnValue({
      data: {
        sessions: [
          makeActiveSession({
            timeline_anchor_at: "2026-03-21T12:03:00Z",
            last_activity_at: "2026-03-21T12:03:00Z",
            presence_updated_at: "2026-03-21T12:03:00Z",
            presence_state: "running",
            presence_tool: "bash",
          }),
        ],
        total: 1,
        last_refresh: "2026-03-21T12:03:00Z",
      },
      isLoading: false,
      error: null,
    });

    const { container } = renderSessionsPage();

    await user.click(await screen.findByRole("button", { name: "Live view" }));

    expect(await screen.findByText("Needs you")).toBeInTheDocument();
    const timelinePhases = Array.from(container.querySelectorAll(".session-card-runtime-phase")).map((node) => node.textContent);
    expect(timelinePhases).toContain("Needs you");
    expect(timelinePhases).not.toContain("Running bash");
  });
});
