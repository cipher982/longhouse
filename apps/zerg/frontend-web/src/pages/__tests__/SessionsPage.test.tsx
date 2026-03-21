import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes } from "react-router-dom";
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

vi.mock("../../hooks/useAgentSessions", () => ({
  useAgentSessions: hookMocks.useAgentSessions,
  useAgentFilters: hookMocks.useAgentFilters,
}));

vi.mock("../../hooks/useActiveSessions", () => ({
  useActiveSessions: activeSessionMocks.useActiveSessions,
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
          <Route path="/timeline" element={<SessionsPage />} />
          <Route path="/briefings" element={<div>Briefings</div>} />
          <Route path="/settings" element={<div>Settings</div>} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>
  );
}

describe("SessionsPage", () => {
  let latestFilters: AgentSessionFilters | undefined;

  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    latestFilters = undefined;

    mockUseAgentSessions.mockImplementation((filters: AgentSessionFilters) => {
      latestFilters = filters;
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
  });

  afterEach(() => {
    vi.useRealTimers();
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

  it("renders live runtime state directly on the main timeline cards", async () => {
    mockUseAgentSessions.mockReturnValue({
      data: {
        sessions: [makeSession({ ended_at: "2026-03-21T12:03:00Z" })],
        total: 1,
        has_real_sessions: true,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    mockUseActiveSessions.mockReturnValue({
      data: {
        sessions: [makeActiveSession()],
        total: 1,
        last_refresh: "2026-03-21T12:04:00Z",
      },
      isLoading: false,
      error: null,
    });

    renderSessionsPage();

    expect(await screen.findByText("Running bash")).toBeInTheDocument();
    expect(screen.getByText("Live now")).toBeInTheDocument();
    expect(screen.queryByText("In progress")).not.toBeInTheDocument();
    expect(mockUseActiveSessions).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: true,
        limit: 50,
      }),
    );
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
});
