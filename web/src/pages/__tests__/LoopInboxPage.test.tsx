import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  AgentSession,
  AgentSessionPreview,
  TimelineSessionCard,
} from "../../services/api/agents";
import { TestRouter } from "../../test/test-utils";
import LoopInboxPage from "../LoopInboxPage";
import {
  connectTimelineSessionsStream,
  fetchAgentSession,
  fetchAgentSessionPreview,
  fetchAgentSessions,
  setSessionAction,
} from "../../services/api/agents";
import { sendLiveSessionMessage } from "../../services/api/sessionChat";
import { useLoopInstallPrompt } from "../../hooks/useLoopInstallPrompt";

vi.mock("../../services/api/agents", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../services/api/agents")>();
  return {
    ...actual,
    fetchAgentSessions: vi.fn(),
    fetchAgentSession: vi.fn(),
    fetchAgentSessionPreview: vi.fn(),
    setSessionAction: vi.fn(),
    connectTimelineSessionsStream: vi.fn(() => () => {}),
  };
});

vi.mock("../../services/api/sessionChat", () => ({
  sendLiveSessionMessage: vi.fn(),
}));

vi.mock("../../hooks/useLoopInstallPrompt", () => ({
  useLoopInstallPrompt: vi.fn(),
}));

const fetchAgentSessionsMock = vi.mocked(fetchAgentSessions);
const fetchAgentSessionMock = vi.mocked(fetchAgentSession);
const fetchAgentSessionPreviewMock = vi.mocked(fetchAgentSessionPreview);
const setSessionActionMock = vi.mocked(setSessionAction);
const connectTimelineSessionsStreamMock = vi.mocked(
  connectTimelineSessionsStream,
);
const sendLiveSessionMessageMock = vi.mocked(sendLiveSessionMessage);
const useLoopInstallPromptMock = vi.mocked(useLoopInstallPrompt);

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="loop-location">{location.pathname}</div>;
}

function makeSession(overrides: Partial<AgentSession> = {}): AgentSession {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    provider: "claude",
    project: "zerg",
    device_id: "cinder",
    environment: "development",
    cwd: "/Users/davidrose/git/zerg",
    git_repo: "git@github.com:cipher982/longhouse.git",
    git_branch: "main",
    started_at: "2026-04-14T10:00:00Z",
    ended_at: null,
    last_activity_at: "2026-04-14T10:03:00Z",
    timeline_anchor_at: "2026-04-14T10:03:00Z",
    runtime_phase: "needs_user",
    phase_started_at: "2026-04-14T10:02:00Z",
    last_progress_at: "2026-04-14T10:03:00Z",
    runtime_source: "progress",
    terminal_state: "idle",
    runtime_version: 3,
    status: "active",
    presence_state: "needs_user",
    presence_tool: null,
    presence_updated_at: "2026-04-14T10:03:00Z",
    last_live_at: "2026-04-14T10:03:00Z",
    display_phase: "needs_user",
    active_tool: null,
    confidence: "live",
    user_messages: 2,
    assistant_messages: 2,
    tool_calls: 1,
    summary:
      "Waiting for the next instruction before continuing the implementation.",
    summary_title: "Loop surface MVP",
    first_user_message: "Build the iPhone Loop MVP",
    thread_root_session_id: "11111111-1111-1111-1111-111111111111",
    thread_head_session_id: "11111111-1111-1111-1111-111111111111",
    thread_continuation_count: 1,
    continued_from_session_id: null,
    continuation_kind: "local",
    origin_label: "cinder",
    home_label: "On this Mac",
    branched_from_event_id: null,
    is_writable_head: true,
    control: {
      managed_transport: "claude_channel_bridge",
      source_runner_id: 7,
      source_runner_name: "cinder",
      attach_command: "longhouse continue 11111111-1111-1111-1111-111111111111",
    },
    capabilities: {
      live_control_available: true,
      host_reattach_available: true,
      reply_to_live_session_available: true,
    },
    loop_mode: "assist",
    user_state: "active",
    ...overrides,
  };
}

function makeCard(session: AgentSession): TimelineSessionCard {
  return {
    thread_id: session.thread_root_session_id,
    timeline_anchor_at: session.timeline_anchor_at ?? session.last_activity_at,
    head: session,
    detail: session,
    root: session,
    continuation_count: session.thread_continuation_count,
    started_origin_label: session.origin_label,
    head_origin_label: session.origin_label,
  };
}

function makePreview(
  overrides: Partial<AgentSessionPreview> = {},
): AgentSessionPreview {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    total_messages: 4,
    messages: [
      {
        role: "user",
        content: "Please stop when you need my go-ahead.",
        timestamp: "2026-04-14T10:01:00Z",
      },
      {
        role: "assistant",
        content:
          "I finished the first slice and I am waiting on your next instruction.",
        timestamp: "2026-04-14T10:02:00Z",
      },
    ],
    ...overrides,
  };
}

function renderPage(initialEntry = "/loop") {
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
            path="/loop"
            element={
              <>
                <LoopInboxPage />
                <LocationProbe />
              </>
            }
          />
          <Route
            path="/loop/:sessionId"
            element={
              <>
                <LoopInboxPage />
                <LocationProbe />
              </>
            }
          />
          <Route
            path="/loop/card/:sessionId"
            element={
              <>
                <LoopInboxPage />
                <LocationProbe />
              </>
            }
          />
          <Route path="/timeline" element={<LocationProbe />} />
          <Route path="/timeline/:sessionId" element={<LocationProbe />} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>,
  );
}

describe("LoopInboxPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("EventSource", class {} as typeof EventSource);

    const actionableSession = makeSession();
    const staleSession = makeSession({
      id: "22222222-2222-2222-2222-222222222222",
      summary_title: "Already idle",
      summary: "This session is no longer waiting.",
      presence_state: "idle",
      runtime_phase: "idle",
      display_phase: "idle",
      capabilities: {
        live_control_available: false,
        host_reattach_available: false,
        reply_to_live_session_available: false,
      },
    });

    fetchAgentSessionsMock.mockResolvedValue({
      sessions: [makeCard(actionableSession), makeCard(staleSession)],
      total: 2,
      has_real_sessions: true,
    });
    fetchAgentSessionMock.mockResolvedValue(actionableSession);
    fetchAgentSessionPreviewMock.mockResolvedValue(makePreview());
    setSessionActionMock.mockResolvedValue({
      session_id: actionableSession.id,
      user_state: "snoozed",
    });
    sendLiveSessionMessageMock.mockResolvedValue({
      accepted: true,
      session_id: actionableSession.id,
      request_id: "req-1",
      dispatch_ms: 10,
    });
    useLoopInstallPromptMock.mockReturnValue({
      canInstall: false,
      showIosHint: false,
      isInstalled: false,
      install: vi.fn(),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("redirects /loop to the first actionable session", async () => {
    renderPage("/loop");

    await waitFor(() => {
      expect(screen.getByTestId("loop-location")).toHaveTextContent(
        "/loop/11111111-1111-1111-1111-111111111111",
      );
    });
  });

  it("renders the selected waiting session and ignores idle sessions in the queue", async () => {
    renderPage("/loop/11111111-1111-1111-1111-111111111111");

    await waitFor(() => {
      expect(
        screen.getByTestId(
          "loop-inbox-row-11111111-1111-1111-1111-111111111111",
        ),
      ).toBeInTheDocument();
    });

    expect(
      screen.queryByTestId(
        "loop-inbox-row-22222222-2222-2222-2222-222222222222",
      ),
    ).not.toBeInTheDocument();
    expect(screen.getAllByText("Loop surface MVP")).toHaveLength(2);
    expect(
      screen.getByText("Please stop when you need my go-ahead."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "I finished the first slice and I am waiting on your next instruction.",
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Waiting on you")).toHaveLength(2);
  });

  it("snoozes the selected session from the phone queue", async () => {
    const user = userEvent.setup();
    renderPage("/loop/11111111-1111-1111-1111-111111111111");

    await waitFor(() => {
      expect(screen.getByTestId("loop-not-now-action")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("loop-not-now-action"));

    await waitFor(() => {
      expect(setSessionActionMock).toHaveBeenCalledWith(
        "11111111-1111-1111-1111-111111111111",
        "snooze",
      );
    });
  });

  it("sends a live reply when the session supports it", async () => {
    const user = userEvent.setup();
    renderPage("/loop/11111111-1111-1111-1111-111111111111");

    await waitFor(() => {
      expect(screen.getByTestId("loop-reply-input")).toBeInTheDocument();
    });

    await user.type(
      screen.getByTestId("loop-reply-input"),
      "Continue with the next slice.",
    );
    await user.click(screen.getByTestId("loop-reply-action"));

    await waitFor(() => {
      expect(sendLiveSessionMessageMock).toHaveBeenCalledWith(
        "11111111-1111-1111-1111-111111111111",
        "Continue with the next slice.",
      );
    });
  });

  it("shows the stale-state banner and resume control for snoozed sessions", async () => {
    const user = userEvent.setup();
    const snoozedSession = makeSession({
      user_state: "snoozed",
      presence_state: "idle",
      runtime_phase: "idle",
      display_phase: "idle",
      capabilities: {
        live_control_available: false,
        host_reattach_available: false,
        reply_to_live_session_available: false,
      },
    });

    fetchAgentSessionMock.mockResolvedValue(snoozedSession);
    setSessionActionMock.mockResolvedValue({
      session_id: snoozedSession.id,
      user_state: "active",
    });

    renderPage("/loop/card/11111111-1111-1111-1111-111111111111");

    await waitFor(() => {
      expect(
        screen.getByTestId("loop-inbox-card-status-banner"),
      ).toHaveTextContent("This session is snoozed");
    });

    await user.click(screen.getByTestId("loop-resume-action"));

    await waitFor(() => {
      expect(setSessionActionMock).toHaveBeenCalledWith(
        "11111111-1111-1111-1111-111111111111",
        "resume",
      );
    });
  });

  it("subscribes to timeline updates for a tighter live loop", async () => {
    renderPage("/loop/11111111-1111-1111-1111-111111111111");

    await waitFor(() => {
      expect(connectTimelineSessionsStreamMock).toHaveBeenCalled();
    });
  });
});
