import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { buildTimelineModel } from "../../lib/sessionWorkspace";
import type { AgentSession, AgentSessionProjectionItem } from "../../services/api/agents";
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
  SessionChat: () => <div data-testid="session-chat">session-chat</div>,
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
    managed_transport: "tmux",
    source_runner_id: 7,
    source_runner_name: "cinder",
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
        filteredItems: model.items,
        eventsLoading: false,
        eventsError: null,
        fetchNextPage: vi.fn(),
        hasNextPage: false,
        isFetchingNextPage: false,
        eventFilter: "all",
        setEventFilter: vi.fn(),
        searchQuery: "",
        setSearchQuery: vi.fn(),
        debouncedSearch: "",
        messageCount: 1,
        toolRowCount: 1,
        outsideActiveCount: 0,
        abandonedEvents: 0,
        showAbandonedBranches: false,
        setShowAbandonedBranches: vi.fn(),
        selectedKey,
        selectedSelection: selectedKey ? model.selectionMap.get(selectedKey) ?? null : null,
        selectKey: setSelectedKey,
        registerTimelineList: vi.fn(),
      };
    });
  });

  it("renders Codex detail with continuation notice and preserved tool inspector labels", async () => {
    const user = userEvent.setup();
    renderSessionDetailPage();

    expect(screen.getByTestId("session-continuation-unavailable")).toHaveTextContent(
      "Web continuation unavailable for Codex",
    );
    expect(screen.queryByTestId("session-chat")).not.toBeInTheDocument();
    expect(screen.getByText("Transcript row from Codex.")).toBeInTheDocument();

    const toolLabel = screen.getByText("Bash");
    const toolRow = toolLabel.closest("button");
    if (!(toolRow instanceof HTMLButtonElement)) {
      throw new Error("Expected the tool label to live inside a clickable row");
    }

    await user.click(toolRow);

    expect(screen.getByText("Output")).toBeInTheDocument();
  });
});
