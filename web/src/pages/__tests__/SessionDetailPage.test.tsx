import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { buildTimelineModel } from "../../lib/sessionWorkspace";
import type {
  AgentSession,
  AgentSessionProjectionItem,
  SessionCapabilities,
  SessionRuntimeDisplay,
  AgentSessionTurn,
} from "../../services/api/agents";
import { TestRouter } from "../../test/test-utils";
import SessionDetailPage from "../SessionDetailPage";

const workspaceMocks = vi.hoisted(() => ({
  useSessionWorkspace: vi.fn(),
}));
const secondClockMocks = vi.hoisted(() => ({
  useSecondClock: vi.fn(),
}));

vi.mock("../../hooks/useSessionWorkspace", () => ({
  useSessionWorkspace: workspaceMocks.useSessionWorkspace,
}));
vi.mock("../../hooks/useSecondClock", () => ({
  useSecondClock: secondClockMocks.useSecondClock,
}));

vi.mock("../../lib/readiness-contract", () => ({
  useReadinessFlag: vi.fn(),
}));

vi.mock("../../components/SessionChat", () => ({
  SessionChat: ({
    composerDisabledReason,
    managedLaunchSuggestion,
  }: {
    composerDisabledReason?: string | null;
    managedLaunchSuggestion?: { command: string } | null;
  }) => (
    <div
      data-testid="session-chat"
      data-disabled-reason={composerDisabledReason ?? ""}
      data-launch-command={managedLaunchSuggestion?.command ?? ""}
    >
      session-chat
    </div>
  ),
}));

async function openInfoDrawer(user: ReturnType<typeof userEvent.setup>) {
  const button = screen.getByTestId("session-info-button");
  await user.click(button);
}

function makeCapabilities(
  overrides: Partial<SessionCapabilities> = {},
): SessionCapabilities {
  return {
    live_control_available: true,
    host_reattach_available: true,
    reply_to_live_session_available: true,
    ...overrides,
  };
}

function makeRuntimeDisplay(
  overrides: Partial<SessionRuntimeDisplay> = {},
): SessionRuntimeDisplay {
  return {
    truth_tier: "managed-local",
    signal_tier: "phase_signal",
    state: "running",
    tone: "running",
    headline: "Working",
    detail: "Using Shell",
    phase_label: "Using Shell",
    compact_tool_label: "Shell",
    is_live: true,
    is_executing: true,
    needs_attention: false,
    is_idle: false,
    is_stalled: false,
    is_managed_local_truth: true,
    has_signal: true,
    control_path: "managed",
    activity_recency: "live",
    lifecycle: "open",
    host_state: "online",
    terminal_reason: null,
    ...overrides,
  };
}

function makeSession(overrides: Partial<AgentSession> = {}): AgentSession {
  return {
    id: "session-codex",
    provider: "codex",
    project: "zerg",
    device_id: "cinder",
    environment: "development",
    cwd: "/Users/davidrose/git/zerg",
    git_repo: "git@github.com:cipher982/longhouse.git",
    git_branch: "main",
    started_at: "2026-03-22T22:00:00Z",
    ended_at: "2026-03-22T22:05:00Z",
    last_activity_at: "2026-03-22T22:05:00Z",
    user_messages: 1,
    assistant_messages: 1,
    tool_calls: 1,
    summary: "Investigated Codex rendering",
    summary_title: "Codex detail verification",
    first_user_message: "Verify the session detail page",
    thread_root_session_id: "session-codex",
    thread_head_session_id: "session-codex",
    thread_continuation_count: 1,
    continued_from_session_id: null,
    continuation_kind: "local",
    origin_label: "On this Mac",
    home_label: "On this Mac",
    branched_from_event_id: null,
    is_writable_head: true,
    control: {
      source_runner_id: 7,
      source_runner_name: "cinder",
      attach_command:
        "zsh -lc 'exec longhouse-engine codex-bridge attach --session-id session-codex'",
    },
    capabilities: makeCapabilities(),
    runtime_display: makeRuntimeDisplay(),
    timeline_card: {
      ownership: {
        label: "Managed",
        tone: "neutral",
      },
      status: {
        label: "Using Shell",
        tone: "running",
        seen_at: "2026-03-22T22:04:30Z",
        seen_at_prefix: "Updated",
      },
      border_tone: "running",
    },
    loop_mode: "assist",
    ...overrides,
  };
}

function renderSessionDetailPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={["/timeline/session-codex"]}>
        <Routes>
          <Route path="/timeline/:sessionId" element={<SessionDetailPage />} />
          <Route path="/timeline" element={<div>Timeline</div>} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>,
  );
}

function makeTurn(overrides: Partial<AgentSessionTurn> = {}): AgentSessionTurn {
  return {
    id: 1,
    session_id: "session-codex",
    request_id: "req-1",
    state: "active",
    terminal_phase: null,
    error_code: null,
    user_event_id: null,
    durable_assistant_event_id: null,
    baseline_event_id: null,
    baseline_observation_cursor: null,
    user_submitted_at: "2026-03-22T22:03:45Z",
    send_accepted_at: "2026-03-22T22:03:46Z",
    active_phase_observed_at: "2026-03-22T22:03:47Z",
    terminal_at: null,
    durable_at: null,
    created_at: "2026-03-22T22:03:45Z",
    updated_at: "2026-03-22T22:03:47Z",
    ...overrides,
  };
}

function mockWorkspaceState({
  session,
  model,
  turns = [],
}: {
  session: AgentSession;
  model: ReturnType<typeof buildTimelineModel>;
  turns?: AgentSessionTurn[];
}) {
  workspaceMocks.useSessionWorkspace.mockImplementation(() => {
    const [selectedKey, setSelectedKey] = React.useState<string | null>(null);
    return {
      session,
      sessionLoading: false,
      sessionError: null,
      turns,
      turnsLoading: false,
      turnsError: null,
      threadSessions: [session],
      currentThreadSession: session,
      headThreadSession: session,
      isViewingHead: true,
      totalEntries: model.items.length,
      loadedEntryCount: model.items.length,
      items: model.items,
      eventsLoading: false,
      eventsError: null,
      fetchPreviousPage: vi.fn(),
      hasPreviousPage: false,
      isFetchingPreviousPage: false,
      abandonedEvents: 0,
      showAbandonedBranches: false,
      setShowAbandonedBranches: vi.fn(),
      selectedKey,
      selectedSelection: selectedKey
        ? (model.selectionMap.get(selectedKey) ?? null)
        : null,
      selectKey: setSelectedKey,
      handleVisibleSelectionChange: vi.fn(),
      registerTimelineList: vi.fn(),
    };
  });
}

describe("SessionDetailPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    secondClockMocks.useSecondClock.mockReturnValue(
      Date.parse("2026-03-22T22:04:30Z"),
    );

    const session = makeSession({
      ended_at: null,
      status: "working",
      presence_state: "running",
      active_tool: "Bash",
      runtime_source: "managed_local_transport",
      confidence: "live",
      display_phase: "Running Bash",
      last_live_at: "2026-03-22T22:04:30Z",
    });
    const projectionItems: AgentSessionProjectionItem[] = [
      {
        kind: "event",
        session_id: session.id,
        timestamp: "2026-03-22T22:00:01Z",
        event: {
          id: 1,
          role: "assistant",
          content_text: "Transcript row from Codex.",
          tool_name: null,
          tool_input_json: null,
          tool_output_text: null,
          tool_call_id: null,
          timestamp: "2026-03-22T22:00:01Z",
          in_active_context: true,
        },
      },
      {
        kind: "event",
        session_id: session.id,
        timestamp: "2026-03-22T22:00:02Z",
        event: {
          id: 2,
          role: "tool",
          content_text: null,
          tool_name: "Bash",
          tool_input_json: null,
          tool_output_text: "README.md",
          tool_call_id: null,
          timestamp: "2026-03-22T22:00:02Z",
          in_active_context: true,
        },
      },
    ];
    const model = buildTimelineModel(projectionItems);
    mockWorkspaceState({ session, model });
  });

  it("renders managed-local Codex detail with live-session controls and preserved tool inspector labels", async () => {
    const user = userEvent.setup();
    renderSessionDetailPage();

    expect(
      screen.queryByTestId("session-continuation-unavailable"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("session-sidebar-runtime"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("session-detail-header-runtime"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("session-control-strip")).toHaveTextContent(
      "Working",
    );
    expect(screen.getByTestId("session-control-strip")).toHaveTextContent(
      "Using Shell",
    );
    expect(document.querySelector(".session-workspace-route")).toHaveClass(
      "session-workspace-route--managed",
      "session-workspace-route--tone-running",
    );
    // Loop mode lives on the composer dock in the new layout.
    expect(screen.getByTestId("session-loop-mode-pill")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Assist" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Autopilot" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    // Drawer-only context (summary / metadata / terminal attach) is hidden by default.
    expect(screen.queryByText("Summary")).not.toBeInTheDocument();
    expect(screen.queryByText("Metadata")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("session-debug-attach"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("render-telemetry-panel"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("session-launch-profile"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("session-chat")).toBeInTheDocument();
    expect(screen.getByTestId("session-chat")).toHaveAttribute(
      "data-disabled-reason",
      "",
    );
    expect(screen.getByTestId("session-chat")).toHaveAttribute(
      "data-launch-command",
      "",
    );
    expect(screen.getByText("Transcript row from Codex.")).toBeInTheDocument();

    await openInfoDrawer(user);
    const drawer = screen.getByTestId("session-info-drawer");
    const summaryTitle = screen.getByText("Summary");
    const metadataTitle = screen.getByText("Metadata");
    const terminalTitle = screen.getByText("Terminal");
    expect(drawer).toContainElement(summaryTitle);
    expect(drawer).toContainElement(metadataTitle);
    expect(drawer).toContainElement(terminalTitle);
    expect(
      summaryTitle.compareDocumentPosition(metadataTitle) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      metadataTitle.compareDocumentPosition(terminalTitle) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.getByTestId("session-debug-attach")).toHaveTextContent(
      "Terminal",
    );
    expect(screen.getByTestId("session-debug-attach")).toHaveTextContent(
      "Attach command",
    );
    expect(screen.getByTestId("session-debug-attach-command")).toHaveTextContent(
      "codex-bridge attach --session-id session-codex",
    );

    const toolLabel = screen.getByText("Bash");
    const toolRow = toolLabel.closest("button");
    if (!(toolRow instanceof HTMLButtonElement)) {
      throw new Error("Expected the tool label to live inside a clickable row");
    }

    await user.click(toolRow);

    // Inline-expanded detail shows an 'output' label (lowercased in the
    // redesigned timeline). The old right-rail inspector with its 'Status'
    // meta-list is gone.
    expect(screen.getByText("output")).toBeInTheDocument();
  });

  it("does not render HEAD as branch metadata", async () => {
    const user = userEvent.setup();
    const session = makeSession({ git_branch: "HEAD" });
    mockWorkspaceState({ session, model: buildTimelineModel([]) });

    renderSessionDetailPage();
    await openInfoDrawer(user);

    expect(screen.getByText("Metadata")).toBeInTheDocument();
    expect(screen.queryByText("Branch")).not.toBeInTheDocument();
    expect(screen.queryByText("HEAD")).not.toBeInTheDocument();
  });

  it("keeps terminal attach in the terminal section when control is offline", async () => {
    const user = userEvent.setup();
    const session = makeSession({
      ended_at: null,
      status: "working",
      presence_state: "running",
      active_tool: "Bash",
      runtime_source: "managed_local_transport",
      confidence: "live",
      display_phase: "Running Bash",
      last_live_at: "2026-03-22T22:04:30Z",
      loop_mode: "assist",
      capabilities: makeCapabilities({
        live_control_available: false,
        host_reattach_available: true,
        reply_to_live_session_available: false,
      }),
    });
    const model = buildTimelineModel([
      {
        kind: "event",
        session_id: session.id,
        timestamp: "2026-03-22T22:00:01Z",
        event: {
          id: 1,
          role: "assistant",
          content_text: "Transcript row from degraded Codex.",
          tool_name: null,
          tool_input_json: null,
          tool_output_text: null,
          tool_call_id: null,
          timestamp: "2026-03-22T22:00:01Z",
          in_active_context: true,
        },
      },
    ]);
    mockWorkspaceState({ session, model });

    renderSessionDetailPage();
    await openInfoDrawer(user);

    expect(
      screen.queryByTestId("session-attach-callout"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("session-debug-attach")).toHaveTextContent(
      "Terminal",
    );
    expect(screen.getByTestId("session-debug-attach")).toHaveTextContent(
      "Attach command",
    );
    expect(screen.getByTestId("session-debug-attach-command")).toHaveTextContent(
      "codex-bridge attach --session-id session-codex",
    );
    const continuationNotice = screen.getByTestId(
      "session-continuation-unavailable",
    );
    expect(continuationNotice).toHaveTextContent("Control is offline");
    expect(screen.getByTestId("session-chat")).toHaveAttribute(
      "data-disabled-reason",
      "Longhouse can see this Codex session, but cannot send prompts until the engine reconnects.",
    );
  });

  it("keeps unresolved live tool calls pending from the row into the inspector", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-03-22T22:04:30Z"));
      const session = makeSession({
        ended_at: null,
        status: "working",
        presence_state: "running",
        active_tool: "Bash",
        runtime_source: "managed_local_transport",
        confidence: "live",
        display_phase: "Running Bash",
        last_live_at: "2026-03-22T22:04:30Z",
      });
      const model = buildTimelineModel([
        {
          kind: "event",
          session_id: session.id,
          timestamp: "2026-03-22T22:04:00Z",
          event: {
            id: 1,
            role: "assistant",
            content_text: null,
            tool_name: "Bash",
            tool_input_json: { command: "git status --short" },
            tool_output_text: null,
            tool_call_id: "tc-pending",
            tool_call_state: "running",
            timestamp: "2026-03-22T22:04:00Z",
            in_active_context: true,
          },
        },
      ]);

      mockWorkspaceState({ session, model });
      renderSessionDetailPage();

      expect(screen.getByTestId("session-control-strip")).toHaveTextContent(
        "Working",
      );
      expect(screen.getByTestId("session-control-strip")).toHaveTextContent(
        "Using Shell",
      );

      {
        const label = screen.getByText("Bash");
        const row = label.closest("[data-row-kind=\"tool\"]");
        expect(row).toHaveTextContent("running");
      }

      const toolLabel = screen.getByText("Bash");
      const toolRow = toolLabel.closest("button");
      if (!(toolRow instanceof HTMLButtonElement)) {
        throw new Error("Expected the tool label to live inside a clickable row");
      }

      fireEvent.click(toolRow);

      const row = screen.getByText("Bash").closest("[data-row-kind=\"tool\"]");
      expect(row).not.toBeNull();
      expect(row).toHaveTextContent("Result not recorded yet.");
      expect(row).not.toHaveTextContent(
        "Tool call dropped \u2014 no result was ever recorded.",
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps backend runtime display detail canonical when pending tool rows exist", () => {
    const session = makeSession({
      ended_at: null,
      status: "working",
      presence_state: "blocked",
      active_tool: "AskUserQuestion",
      runtime_source: "managed_local_transport",
      confidence: "live",
      display_phase: "Blocked AskUserQuestion",
      last_live_at: "2026-03-22T22:04:30Z",
      runtime_display: makeRuntimeDisplay({
        state: "blocked",
        tone: "blocked",
        headline: "Needs permission",
        detail: "Approval needed • AskUserQuestion",
        phase_label: "Blocked on AskUserQuestion",
        compact_tool_label: "AskUserQuestion",
        is_executing: true,
        needs_attention: true,
      }),
    });
    const model = buildTimelineModel([
      {
        kind: "event",
        session_id: session.id,
        timestamp: "2026-03-22T22:04:00Z",
        event: {
          id: 1,
          role: "assistant",
          content_text: null,
          tool_name: "AskUserQuestion",
          tool_input_json: {
            question: "Which path?",
            choices: ["A", "B"],
          },
          tool_output_text: null,
          tool_call_id: "tc-pending-ask",
          timestamp: "2026-03-22T22:04:00Z",
          in_active_context: true,
        },
      },
    ]);

    mockWorkspaceState({ session, model });
    renderSessionDetailPage();

    const strip = screen.getByTestId("session-control-strip");
    expect(strip).toHaveTextContent("Needs permission");
    expect(strip).toHaveTextContent("Approval needed • AskUserQuestion");
    expect(strip).not.toHaveTextContent("Running AskUserQuestion");
  });

  it("renders the backend launch lifecycle pending state", () => {
    const session = makeSession({
      ended_at: null,
      launch_state: "launching_unknown",
      launch_error_code: null,
      launch_error_message: "transport timed out",
    });

    mockWorkspaceState({ session, model: buildTimelineModel([]) });
    renderSessionDetailPage();

    const banner = screen.getByTestId("launch-pending-banner");
    expect(banner).toHaveTextContent("Starting session on cinder");
    expect(banner).toHaveTextContent("waiting for the machine to confirm");
    expect(screen.queryByTestId("launch-failed-banner")).not.toBeInTheDocument();
  });

  it("renders the backend launch lifecycle failure reason", () => {
    const session = makeSession({
      ended_at: "2026-03-22T22:05:00Z",
      launch_state: "launch_orphaned",
      launch_error_code: "launch_timeout",
      launch_error_message: "Machine Agent did not report back before lease expired",
    });

    mockWorkspaceState({ session, model: buildTimelineModel([]) });
    renderSessionDetailPage();

    const banner = screen.getByTestId("launch-failed-banner");
    expect(banner).toHaveTextContent("Launch failed");
    expect(banner).toHaveTextContent("launch_timeout");
    expect(banner).toHaveTextContent(
      "Machine Agent did not report back before lease expired",
    );
    expect(screen.queryByTestId("launch-pending-banner")).not.toBeInTheDocument();
  });

  it("marks unresolved ended-session tool calls as dropped in both row and inspector", () => {
    const session = makeSession({
      ended_at: "2026-03-22T22:05:00Z",
      status: "completed",
      presence_state: "idle",
      active_tool: null,
      runtime_source: "managed_local_transport",
      confidence: "stale",
      display_phase: "Completed",
      last_live_at: "2026-03-22T22:05:00Z",
    });
    const model = buildTimelineModel([
      {
        kind: "event",
        session_id: session.id,
        timestamp: "2026-03-22T22:04:00Z",
        event: {
          id: 1,
          role: "assistant",
          content_text: null,
          tool_name: "Bash",
          tool_input_json: null,
          tool_output_text: null,
          tool_call_id: "tc-dropped",
          tool_call_state: "dropped",
          timestamp: "2026-03-22T22:04:00Z",
          in_active_context: true,
        },
      },
    ]);

    mockWorkspaceState({ session, model });
    renderSessionDetailPage();

    {
      const label = screen.getByText("Bash");
      const row = label.closest("[data-row-kind=\"tool\"]");
      expect(row).toHaveTextContent("dropped");
    }

    const toolLabel = screen.getByText("Bash");
    const toolRow = toolLabel.closest("button");
    if (!(toolRow instanceof HTMLButtonElement)) {
      throw new Error("Expected the tool label to live inside a clickable row");
    }

    fireEvent.click(toolRow);

    const row = screen.getByText("Bash").closest("[data-row-kind=\"tool\"]");
    expect(row).not.toBeNull();
    expect(row).toHaveTextContent(
      "Tool call dropped \u2014 no result was ever recorded.",
    );
    expect(row).not.toHaveTextContent("Result not recorded yet.");
  });

  it("shows the active turn elapsed counter in the header and control strip", () => {
    const session = makeSession({
      ended_at: null,
      status: "working",
      presence_state: "running",
      active_tool: "Bash",
      runtime_source: "managed_local_transport",
      confidence: "live",
      display_phase: "Running Bash",
      last_live_at: "2026-03-22T22:04:30Z",
    });
    const model = buildTimelineModel([
      {
        kind: "event",
        session_id: session.id,
        timestamp: "2026-03-22T22:00:01Z",
        event: {
          id: 1,
          role: "assistant",
          content_text: "Still working.",
          tool_name: null,
          tool_input_json: null,
          tool_output_text: null,
          tool_call_id: null,
          timestamp: "2026-03-22T22:00:01Z",
          in_active_context: true,
        },
      },
    ]);

    workspaceMocks.useSessionWorkspace.mockImplementation(() => {
      const [selectedKey, setSelectedKey] = React.useState<string | null>(null);
      return {
        session,
        sessionLoading: false,
        sessionError: null,
        turns: [makeTurn({ session_id: session.id })],
        turnsLoading: false,
        turnsError: null,
        threadSessions: [session],
        currentThreadSession: session,
        headThreadSession: session,
        isViewingHead: true,
        totalEntries: model.items.length,
        loadedEntryCount: model.items.length,
        items: model.items,
        eventsLoading: false,
        eventsError: null,
        fetchPreviousPage: vi.fn(),
        hasPreviousPage: false,
        isFetchingPreviousPage: false,
        abandonedEvents: 0,
        showAbandonedBranches: false,
        setShowAbandonedBranches: vi.fn(),
        selectedKey,
        selectedSelection: selectedKey
          ? (model.selectionMap.get(selectedKey) ?? null)
          : null,
        selectKey: setSelectedKey,
        handleVisibleSelectionChange: vi.fn(),
        registerTimelineList: vi.fn(),
      };
    });

    renderSessionDetailPage();

    expect(
      screen.queryByTestId("session-detail-header-runtime"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("session-control-strip")).toHaveTextContent(
      "Turn 00:45",
    );
  });

  it("keeps managed waiting states explicit in the dock", () => {
    const session = makeSession({
      ended_at: null,
      status: "idle",
      presence_state: "needs_user",
      active_tool: null,
      runtime_source: "managed_local_transport",
      confidence: "live",
      display_phase: "Idle",
      last_live_at: "2026-03-22T22:04:30Z",
      runtime_display: makeRuntimeDisplay({
        state: "needs_user",
        tone: "idle",
        headline: "Idle",
        detail: "Waiting for next prompt",
        phase_label: "Idle",
        compact_tool_label: null,
        is_live: false,
        is_executing: false,
        is_idle: true,
      }),
    });
    const model = buildTimelineModel([
      {
        kind: "event",
        session_id: session.id,
        timestamp: "2026-03-22T22:00:01Z",
        event: {
          id: 1,
          role: "assistant",
          content_text: "Need one follow-up from the user.",
          tool_name: null,
          tool_input_json: null,
          tool_output_text: null,
          tool_call_id: null,
          timestamp: "2026-03-22T22:00:01Z",
          in_active_context: true,
        },
      },
    ]);

    workspaceMocks.useSessionWorkspace.mockImplementation(() => {
      const [selectedKey, setSelectedKey] = React.useState<string | null>(null);
      return {
        session,
        sessionLoading: false,
        sessionError: null,
        turns: [],
        turnsLoading: false,
        turnsError: null,
        threadSessions: [session],
        currentThreadSession: session,
        headThreadSession: session,
        isViewingHead: true,
        totalEntries: model.items.length,
        loadedEntryCount: model.items.length,
        items: model.items,
        eventsLoading: false,
        eventsError: null,
        fetchPreviousPage: vi.fn(),
        hasPreviousPage: false,
        isFetchingPreviousPage: false,
        abandonedEvents: 0,
        showAbandonedBranches: false,
        setShowAbandonedBranches: vi.fn(),
        selectedKey,
        selectedSelection: selectedKey
          ? (model.selectionMap.get(selectedKey) ?? null)
          : null,
        selectKey: setSelectedKey,
        handleVisibleSelectionChange: vi.fn(),
        registerTimelineList: vi.fn(),
      };
    });

    renderSessionDetailPage();

    expect(
      screen.queryByTestId("session-sidebar-runtime"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("session-detail-header-runtime"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("session-control-strip")).toHaveTextContent("Idle");
    expect(screen.getByTestId("session-control-strip")).toHaveTextContent(
      "Waiting for next prompt",
    );
  });

  it("uses explicit runtime facts for the dock presence marker", () => {
    const session = makeSession({
      ended_at: null,
      status: "working",
      presence_state: null,
      active_tool: null,
      runtime_source: null,
      confidence: null,
      display_phase: null,
      last_live_at: null,
    });
    mockWorkspaceState({ session, model: buildTimelineModel([]) });

    renderSessionDetailPage();

    expect(screen.getByTestId("session-control-strip")).toHaveTextContent(
      "Using Shell",
    );
    expect(screen.getByTitle("Running: Shell")).toBeInTheDocument();
  });

  it("does not promote recent transcript timestamps into live runtime tone", () => {
    const session = makeSession({
      home_label: null,
      control: null,
      ended_at: null,
      last_activity_at: "2026-03-22T22:04:29Z",
      timeline_anchor_at: "2026-03-22T22:04:29Z",
      status: null,
      presence_state: null,
      active_tool: null,
      runtime_source: null,
      confidence: null,
      display_phase: null,
      last_live_at: null,
      runtime_display: makeRuntimeDisplay({
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
        is_idle: true,
        is_managed_local_truth: false,
        has_signal: false,
        control_path: "unmanaged",
        host_state: null,
      }),
      capabilities: makeCapabilities({
        live_control_available: false,
        host_reattach_available: false,
        reply_to_live_session_available: false,
      }),
    });
    mockWorkspaceState({ session, model: buildTimelineModel([]) });

    renderSessionDetailPage();

    expect(document.querySelector(".session-workspace-route")).toHaveClass(
      "session-workspace-route--unmanaged",
      "session-workspace-route--tone-inactive",
    );
    expect(screen.getByTestId("session-control-strip")).toHaveTextContent(
      "Inactive",
    );
  });

  it("keeps the dock visible for searchable-only sessions and explains the search-only state", async () => {
    const session = makeSession({
      provider: "gemini",
      home_label: null,
      control: null,
      continuation_kind: "local",
      id: "session-gemini",
      ended_at: null,
      runtime_source: "progress",
      confidence: "stale",
      display_phase: "Running",
      last_live_at: "2026-03-22T22:05:00Z",
      runtime_display: makeRuntimeDisplay({
        truth_tier: "stale",
        signal_tier: "transcript_progress",
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
        is_managed_local_truth: false,
        has_signal: true,
        control_path: "unmanaged",
        activity_recency: "stale",
        lifecycle: "open",
        host_state: "unknown",
      }),
      capabilities: makeCapabilities({
        live_control_available: false,
        host_reattach_available: false,
        reply_to_live_session_available: false,
      }),
    });
    const model = buildTimelineModel([
      {
        kind: "event",
        session_id: session.id,
        timestamp: "2026-03-22T22:00:01Z",
        event: {
          id: 1,
          role: "assistant",
          content_text: "Transcript row from Gemini.",
          tool_name: null,
          tool_input_json: null,
          tool_output_text: null,
          tool_call_id: null,
          timestamp: "2026-03-22T22:00:01Z",
          in_active_context: true,
        },
      },
    ]);

    workspaceMocks.useSessionWorkspace.mockImplementation(() => {
      const [selectedKey, setSelectedKey] = React.useState<string | null>(null);
      return {
        session,
        sessionLoading: false,
        sessionError: null,
        turns: [],
        turnsLoading: false,
        turnsError: null,
        threadSessions: [session],
        currentThreadSession: session,
        headThreadSession: session,
        isViewingHead: true,
        totalEntries: model.items.length,
        loadedEntryCount: model.items.length,
        items: model.items,
        eventsLoading: false,
        eventsError: null,
        fetchPreviousPage: vi.fn(),
        hasPreviousPage: false,
        isFetchingPreviousPage: false,
        abandonedEvents: 0,
        showAbandonedBranches: false,
        setShowAbandonedBranches: vi.fn(),
        selectedKey,
        selectedSelection: selectedKey
          ? (model.selectionMap.get(selectedKey) ?? null)
          : null,
        selectKey: setSelectedKey,
        handleVisibleSelectionChange: vi.fn(),
        registerTimelineList: vi.fn(),
      };
    });

    renderSessionDetailPage();

    expect(screen.getByTestId("session-chat")).toBeInTheDocument();
    const disabledReason =
      screen.getByTestId("session-chat").getAttribute("data-disabled-reason") ?? "";
    expect(disabledReason).toMatch(/Gemini/);
    expect(disabledReason.toLowerCase()).toMatch(/unmanaged|read-only|cannot/);
    expect(screen.getByTestId("session-chat")).toHaveAttribute(
      "data-launch-command",
      "",
    );
    expect(
      screen.queryByTestId("session-continuation-unavailable"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("session-control-strip")).toHaveTextContent(
      "Read only",
    );
    expect(screen.getByTestId("session-control-strip")).toHaveTextContent(
      "Inactive",
    );
    expect(document.querySelector(".session-workspace-route")).toHaveClass(
      "session-workspace-route--unmanaged",
      "session-workspace-route--tone-inactive",
    );
  });

  it("shows a compact managed-launch hint for imported Codex sessions", async () => {
    const session = makeSession({
      provider: "codex",
      home_label: null,
      control: null,
      continuation_kind: "local",
      id: "session-unmanaged-codex",
      runtime_display: makeRuntimeDisplay({
        truth_tier: "stale",
        signal_tier: "transcript_progress",
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
        is_managed_local_truth: false,
        has_signal: true,
        control_path: "unmanaged",
        activity_recency: "stale",
        lifecycle: "open",
        host_state: "unknown",
      }),
      capabilities: makeCapabilities({
        live_control_available: false,
        host_reattach_available: false,
        reply_to_live_session_available: false,
      }),
    });
    const model = buildTimelineModel([
      {
        kind: "event",
        session_id: session.id,
        timestamp: "2026-03-22T22:00:01Z",
        event: {
          id: 1,
          role: "assistant",
          content_text: "Transcript row from unmanaged Codex.",
          tool_name: null,
          tool_input_json: null,
          tool_output_text: null,
          tool_call_id: null,
          timestamp: "2026-03-22T22:00:01Z",
          in_active_context: true,
        },
      },
    ]);

    workspaceMocks.useSessionWorkspace.mockImplementation(() => {
      const [selectedKey, setSelectedKey] = React.useState<string | null>(null);
      return {
        session,
        sessionLoading: false,
        sessionError: null,
        turns: [],
        turnsLoading: false,
        turnsError: null,
        threadSessions: [session],
        currentThreadSession: session,
        headThreadSession: session,
        isViewingHead: true,
        totalEntries: model.items.length,
        loadedEntryCount: model.items.length,
        items: model.items,
        eventsLoading: false,
        eventsError: null,
        fetchPreviousPage: vi.fn(),
        hasPreviousPage: false,
        isFetchingPreviousPage: false,
        abandonedEvents: 0,
        showAbandonedBranches: false,
        setShowAbandonedBranches: vi.fn(),
        selectedKey,
        selectedSelection: selectedKey
          ? (model.selectionMap.get(selectedKey) ?? null)
          : null,
        selectKey: setSelectedKey,
        handleVisibleSelectionChange: vi.fn(),
        registerTimelineList: vi.fn(),
      };
    });

    const user = userEvent.setup();
    renderSessionDetailPage();
    await openInfoDrawer(user);

    expect(screen.getByTestId("session-managed-launch-hint")).toHaveTextContent(
      "Start the next Codex session through Longhouse",
    );
    expect(
      screen.getByTestId("session-managed-launch-hint-command"),
    ).toHaveTextContent("longhouse codex");
    expect(screen.getByTestId("session-chat")).toHaveAttribute(
      "data-disabled-reason",
      "This unmanaged Codex session is read-only in Longhouse.",
    );
    expect(screen.getByTestId("session-chat")).toHaveAttribute(
      "data-launch-command",
      "",
    );
    expect(
      screen.queryByTestId("session-continuation-unavailable"),
    ).not.toBeInTheDocument();
  });
});
