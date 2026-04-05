import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { buildTimelineModel } from "../../lib/sessionWorkspace";
import type { AgentSession, AgentSessionProjectionItem, SessionCapabilities } from "../../services/api/agents";
import { TestRouter } from "../../test/test-utils";
import SessionDetailPage from "../SessionDetailPage";

const workspaceMocks = vi.hoisted(() => ({
  useSessionWorkspace: vi.fn(),
}));

vi.mock("../../hooks/useSessionWorkspace", () => ({
  useSessionWorkspace: workspaceMocks.useSessionWorkspace,
}));

vi.mock("../../lib/readiness-contract", () => ({
  useReadinessFlag: vi.fn(),
}));

vi.mock("../../services/api/oikos", () => ({
  fetchSessionTurnTelemetry: vi.fn().mockResolvedValue({ latestReview: null }),
}));

vi.mock("../../components/SessionChat", () => ({
  SessionChat: ({
    composerDisabledReason,
  }: {
    composerDisabledReason?: string | null;
  }) => (
    <div
      data-testid="session-chat"
      data-disabled-reason={composerDisabledReason ?? ""}
    >
      session-chat
    </div>
  ),
}));

vi.mock("../../components/workspace/WorkspaceShell", () => ({
  WorkspaceShell: ({
    header,
    sidebar,
    main,
    inspector,
  }: {
    header: React.ReactNode;
    sidebar: React.ReactNode;
    main: React.ReactNode;
    inspector?: React.ReactNode;
  }) => (
    <div data-testid="workspace-shell">
      <div>{header}</div>
      <div>{sidebar}</div>
      <div>{main}</div>
      <div>{inspector}</div>
    </div>
  ),
}));

function makeCapabilities(overrides: Partial<SessionCapabilities> = {}): SessionCapabilities {
  return {
    live_control_available: true,
    cloud_continuation_available: false,
    host_reattach_available: true,
    reply_to_live_session_available: true,
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
    execution_home: "managed_local",
    branched_from_event_id: null,
    is_writable_head: true,
    managed_transport: "codex_app_server",
    source_runner_id: 7,
    source_runner_name: "cinder",
    attach_command: "zsh -lc 'exec longhouse-engine codex-bridge attach --session-id session-codex'",
    managed_launch_profile: {
      required_commands: ["codex"],
      exported_env_keys: ["LONGHOUSE_MANAGED_SESSION_ID", "LONGHOUSE_HOOK_URL", "LONGHOUSE_HOOK_TOKEN"],
      argv: ["codex", "chat", "--session", "<provider-session-id>"],
    },
    capabilities: makeCapabilities(),
    loop_mode: "manual",
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

describe("SessionDetailPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    const session = makeSession();
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

    workspaceMocks.useSessionWorkspace.mockImplementation(() => {
      const [selectedKey, setSelectedKey] = React.useState<string | null>(null);
      return {
        session,
        sessionLoading: false,
        sessionError: null,
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
        selectedSelection: selectedKey ? model.selectionMap.get(selectedKey) ?? null : null,
        selectKey: setSelectedKey,
        handleVisibleSelectionChange: vi.fn(),
        registerTimelineList: vi.fn(),
      };
    });
  });

  it("renders managed-local Codex detail with live-session controls and preserved tool inspector labels", async () => {
    const user = userEvent.setup();
    renderSessionDetailPage();

    expect(screen.queryByTestId("session-continuation-unavailable")).not.toBeInTheDocument();
    expect(screen.getAllByText("Live control")).toHaveLength(2);
    expect(screen.getByTestId("session-capability-summary")).toHaveTextContent(
      "Message this live Codex session from Longhouse, or reattach on the host machine.",
    );
    expect(screen.getByTestId("session-attach-callout")).toHaveTextContent("Reattach the live Codex terminal");
    expect(screen.getByTestId("session-attach-command")).toHaveTextContent(
      "codex-bridge attach --session-id session-codex",
    );
    expect(screen.getByTestId("session-launch-profile")).toHaveTextContent("Managed-local launcher contract");
    expect(screen.getByTestId("session-launch-profile-argv")).toHaveTextContent(
      "codex chat --session <provider-session-id>",
    );
    expect(screen.getByTestId("session-attach-callout")).toHaveTextContent(
      "send prompts from Longhouse below",
    );
    expect(
      screen.getByText(/Keep driving the live session from Longhouse below or by reattaching on the host machine/i),
    ).toBeInTheDocument();
    expect(screen.getByTestId("session-chat")).toBeInTheDocument();
    expect(screen.getByTestId("session-chat")).toHaveAttribute("data-disabled-reason", "");
    expect(screen.getByText("Transcript row from Codex.")).toBeInTheDocument();

    const toolLabel = screen.getByText("Bash");
    const toolRow = toolLabel.closest("button");
    if (!(toolRow instanceof HTMLButtonElement)) {
      throw new Error("Expected the tool label to live inside a clickable row");
    }

    await user.click(toolRow);

    expect(screen.getByText("Output")).toBeInTheDocument();
  });

  it("keeps the dock visible for searchable-only sessions and explains why continuation is disabled", () => {
    const session = makeSession({
      provider: "gemini",
      execution_home: "local",
      managed_transport: null,
      source_runner_id: null,
      source_runner_name: null,
      attach_command: null,
      continuation_kind: "local",
      id: "session-gemini",
      capabilities: makeCapabilities({
        live_control_available: false,
        cloud_continuation_available: false,
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
        selectedSelection: selectedKey ? model.selectionMap.get(selectedKey) ?? null : null,
        selectKey: setSelectedKey,
        handleVisibleSelectionChange: vi.fn(),
        registerTimelineList: vi.fn(),
      };
    });

    renderSessionDetailPage();

    expect(screen.getByTestId("session-chat")).toBeInTheDocument();
    expect(screen.getByTestId("session-chat")).toHaveAttribute(
      "data-disabled-reason",
      "This Gemini session is still fully searchable here, but cloud continuation is not available from this session yet.",
    );
    expect(screen.getByTestId("session-continuation-unavailable")).toHaveTextContent(
      "Web continuation unavailable for Gemini",
    );
  });
});
