import React from "react";
import { describe, beforeAll, afterAll, beforeEach, afterEach, test, expect, vi } from "vitest";
import { render, screen, within, waitFor, fireEvent, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import DashboardPage from "../DashboardPage";
import { TestRouter } from "../../test/test-utils";
import { ConfirmProvider } from "../../components/confirm";
import {
  fetchDashboardSnapshot,
  createAgent,
  runAgent,
  type AgentSummary,
  type AgentRun,
  type DashboardSnapshot,
} from "../../services/api";

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return {
    ...actual,
    fetchDashboardSnapshot: vi.fn(),
    createAgent: vi.fn(),
    resetAgent: vi.fn(),
    runAgent: vi.fn(),
  };
});

type MockWebSocketInstance = {
  onmessage: ((event: MessageEvent) => void) | null;
  close: () => void;
};

function buildAgent(
  overrides: Partial<AgentSummary> & Pick<AgentSummary, "id" | "name" | "status" | "owner_id">
): AgentSummary {
  const now = new Date().toISOString();
  return {
    id: overrides.id,
    name: overrides.name,
    status: overrides.status,
    owner_id: overrides.owner_id,
    owner: overrides.owner ?? null,
    system_instructions: overrides.system_instructions ?? "",
    task_instructions: overrides.task_instructions ?? "",
    model: overrides.model ?? "gpt-5.2-chat-latest",
    schedule: overrides.schedule ?? null,
    config: overrides.config ?? null,
    last_error: overrides.last_error ?? null,
    allowed_tools: overrides.allowed_tools ?? [],
    created_at: overrides.created_at ?? now,
    updated_at: overrides.updated_at ?? now,
    messages: overrides.messages ?? [],
    next_run_at: overrides.next_run_at ?? null,
    last_run_at: overrides.last_run_at ?? null,
  };
}

describe("DashboardPage", () => {
  const fetchDashboardSnapshotMock = fetchDashboardSnapshot as unknown as vi.MockedFunction<typeof fetchDashboardSnapshot>;
  const createAgentMock = createAgent as unknown as vi.MockedFunction<typeof createAgent>;
  const runAgentMock = runAgent as unknown as vi.MockedFunction<typeof runAgent>;
  const mockSockets: MockWebSocketInstance[] = [];

  beforeAll(() => {
    class MockWebSocket {
      public onmessage: ((event: MessageEvent) => void) | null = null;
      public onopen: ((event: Event) => void) | null = null;
      public onclose: ((event: Event) => void) | null = null;
      public onerror: ((event: Event) => void) | null = null;
      public static OPEN = 1;
      public readyState = MockWebSocket.OPEN;
      public send = vi.fn<(data: string) => void>();

      constructor() {
        mockSockets.push(this);
      }

      close() {
        // no-op
      }
    }

    vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
  });

  afterAll(() => {
    vi.unstubAllGlobals();
  });

  beforeEach(() => {
    mockSockets.length = 0;
    fetchDashboardSnapshotMock.mockReset();
    createAgentMock.mockReset();
    runAgentMock.mockReset();
    runAgentMock.mockResolvedValue(undefined);
  });

  afterEach(() => {
    cleanup();
    window.localStorage.clear();
  });

  function renderDashboard(initialAgents: AgentSummary[], runsByAgent?: Record<number, AgentRun[]>) {
    const runsLookup = runsByAgent ?? {};
    const snapshot: DashboardSnapshot = {
      scope: "my",
      fetchedAt: new Date().toISOString(),
      runsLimit: 50,
      agents: initialAgents,
      runs: initialAgents.map((agent) => ({
        agentId: agent.id,
        runs: runsLookup[agent.id] ?? [],
      })),
    };

    fetchDashboardSnapshotMock.mockResolvedValue(snapshot);

    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false, cacheTime: 0 },
      },
    });

    return render(
      <QueryClientProvider client={queryClient}>
        <ConfirmProvider>
          <TestRouter>
            <DashboardPage />
          </TestRouter>
        </ConfirmProvider>
      </QueryClientProvider>
    );
  }

  test("renders dashboard header and agents table", async () => {
    const agents: AgentSummary[] = [
      buildAgent({
        id: 1,
        name: "Alpha",
        status: "running",
        owner_id: 7,
        owner: {
          id: 7,
          email: "alpha@example.com",
          display_name: "Ada",
          is_active: true,
          created_at: new Date().toISOString(),
          avatar_url: "https://example.com/avatar.png",
          prefs: {},
        },
        last_run_at: "2025-09-24T10:00:00.000Z",
        next_run_at: "2025-09-24T12:00:00.000Z",
      }),
      buildAgent({
        id: 2,
        name: "Beta",
        status: "error",
        owner_id: 9,
        owner: null,
        last_run_at: null,
        next_run_at: null,
        last_error: "Failed to execute",
      }),
    ];

    renderDashboard(agents);

    await screen.findByText("Alpha");

    expect(screen.getByRole("button", { name: /Create Agent/i })).toBeInTheDocument();

    const allRows = screen.getAllByRole("row");
    const [headerRow, ...agentRows] = allRows;
    expect(within(headerRow).getByText("Name")).toBeInTheDocument();
    expect(within(headerRow).getByText("Status")).toBeInTheDocument();

    expect(agentRows).toHaveLength(2);
    expect(within(agentRows[0]).getByText("Alpha")).toBeInTheDocument();
    expect(within(agentRows[1]).getByText("Beta")).toBeInTheDocument();
  });

  test("expands an agent row and shows run history", async () => {
    const agent = buildAgent({
      id: 1,
      name: "Runner",
      status: "idle",
      owner_id: 1,
      owner: null,
    });

    const runs: AgentRun[] = [
      {
        id: 42,
        agent_id: 1,
        thread_id: 9,
        status: "success",
        trigger: "manual",
        started_at: "2025-09-24T09:55:00.000Z",
        finished_at: "2025-09-24T09:56:00.000Z",
        duration_ms: 60000,
        total_tokens: 120,
        total_cost_usd: 0.12,
        error: null,
      },
      {
        id: 43,
        agent_id: 1,
        thread_id: 10,
        status: "failed",
        trigger: "schedule",
        started_at: "2025-09-24T08:00:00.000Z",
        finished_at: "2025-09-24T08:01:00.000Z",
        duration_ms: 60000,
        total_tokens: null,
        total_cost_usd: null,
        error: "Timed out",
      },
      {
        id: 44,
        agent_id: 1,
        thread_id: 11,
        status: "success",
        trigger: "manual",
        started_at: "2025-09-24T07:00:00.000Z",
        finished_at: "2025-09-24T07:01:00.000Z",
        duration_ms: 60000,
        total_tokens: 95,
        total_cost_usd: 0.09,
        error: null,
      },
      {
        id: 45,
        agent_id: 1,
        thread_id: 12,
        status: "success",
        trigger: "schedule",
        started_at: "2025-09-24T06:00:00.000Z",
        finished_at: "2025-09-24T06:01:00.000Z",
        duration_ms: 60000,
        total_tokens: 110,
        total_cost_usd: 0.11,
        error: null,
      },
      {
        id: 46,
        agent_id: 1,
        thread_id: 13,
        status: "running",
        trigger: "manual",
        started_at: "2025-09-24T05:00:00.000Z",
        finished_at: null,
        duration_ms: null,
        total_tokens: null,
        total_cost_usd: null,
        error: null,
      },
      {
        id: 47,
        agent_id: 1,
        thread_id: 14,
        status: "success",
        trigger: "manual",
        started_at: "2025-09-24T04:00:00.000Z",
        finished_at: "2025-09-24T04:01:00.000Z",
        duration_ms: 60000,
        total_tokens: 100,
        total_cost_usd: 0.1,
        error: null,
      },
    ];

    renderDashboard([agent], { 1: runs });

    const row = await screen.findByRole("row", { name: /Runner/ });
    await userEvent.click(row);

    await waitFor(() => expect(fetchDashboardSnapshotMock).toHaveBeenCalledTimes(1));

    await screen.findByText("Show all (6)");
    const tables = screen.getAllByRole("table");
    expect(tables.length).toBeGreaterThan(1);
    // Run history table uses SVG icons (CheckCircleIcon) instead of text
    // Query for table rows to verify run data is displayed
    const runHistoryTable = tables[1];
    const rows = within(runHistoryTable).getAllByRole("row");
    // Should have at least header row + some data rows (we have 6 runs in test data)
    expect(rows.length).toBeGreaterThan(1);

    await userEvent.click(screen.getByText("Show all (6)"));
    expect(screen.getByText("Show less")).toBeInTheDocument();
  });

  test("sorts agents by status and toggles sort direction", async () => {
    const agents: AgentSummary[] = [
      buildAgent({ id: 1, name: "Alpha", status: "idle", owner_id: 1 }),
      buildAgent({ id: 2, name: "Beta", status: "running", owner_id: 1 }),
    ];

    renderDashboard(agents);

    const rows = await screen.findAllByRole("row");
    expect(rows[1]).toHaveTextContent("Alpha");

    const statusHeader = document.querySelector<HTMLElement>('[data-column="status"]');
    expect(statusHeader).not.toBeNull();
    if (!statusHeader) {
      throw new Error("Status header not found");
    }
    fireEvent.click(statusHeader);
    await waitFor(() => {
      expect(window.localStorage.getItem("dashboard_sort_key")).toBe("status");
    });

    await waitFor(() => {
      const rowOrder = Array.from(document.querySelectorAll<HTMLElement>('[data-agent-id]'))
        .map((row) => row.getAttribute("data-agent-id"))
        .slice(0, agents.length);
      expect(rowOrder).toEqual(["2", "1"]);
    });

    fireEvent.click(statusHeader);

    await waitFor(() => {
      const rowOrder = Array.from(document.querySelectorAll<HTMLElement>('[data-agent-id]'))
        .map((row) => row.getAttribute("data-agent-id"))
        .slice(0, agents.length);
      expect(rowOrder).toEqual(["1", "2"]);
    });
  });

  test("applies agent status updates from websocket events", async () => {
    const agent = buildAgent({
      id: 42,
      name: "Speedy",
      status: "idle",
      owner_id: 9,
    });

    renderDashboard([agent]);

    // Ensure agent row rendered
    await screen.findByText("Speedy");
    const socket = mockSockets[0];
    expect(socket).toBeDefined();

    // Simulate successful websocket connection
    socket.onopen?.(new Event("open"));

    // Wait for subscribe message to be sent
    await waitFor(() => {
      expect(socket.send).toHaveBeenCalledWith(expect.stringContaining("\"type\":\"subscribe\""));
    });

    const statusCell = document.querySelector<HTMLElement>('[data-agent-id="42"] [data-label="Status"]');
    expect(statusCell).not.toBeNull();
    if (!statusCell) {
      throw new Error("Status cell not found");
    }
    expect(statusCell.textContent).toContain("Idle");

    const payload = {
      type: "agent_updated",
      topic: "agent:42",
      data: {
        id: 42,
        status: "running",
        last_error: null,
        last_run_at: "2025-11-08T23:59:00.000Z",
        next_run_at: null,
      },
    };

    socket.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);

    await waitFor(() => {
      expect(statusCell.textContent).toContain("Running");
    });
  });
});
