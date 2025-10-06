import React from "react";
import { describe, beforeAll, afterAll, beforeEach, afterEach, test, expect, vi } from "vitest";
import { render, screen, within, waitFor, fireEvent, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
import DashboardPage from "../DashboardPage";
import {
  fetchAgents,
  createAgent,
  fetchAgentRuns,
  runAgent,
  type AgentSummary,
  type AgentRun,
} from "../../services/api";

vi.mock("../../services/api", () => ({
  fetchAgents: vi.fn(),
  createAgent: vi.fn(),
  fetchAgentRuns: vi.fn(),
  resetAgent: vi.fn(),
  runAgent: vi.fn(),
}));

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
    model: overrides.model ?? "gpt-4o",
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
  const fetchAgentsMock = fetchAgents as unknown as vi.MockedFunction<typeof fetchAgents>;
  const createAgentMock = createAgent as unknown as vi.MockedFunction<typeof createAgent>;
  const fetchAgentRunsMock = fetchAgentRuns as unknown as vi.MockedFunction<typeof fetchAgentRuns>;
  const runAgentMock = runAgent as unknown as vi.MockedFunction<typeof runAgent>;
  const mockSockets: MockWebSocketInstance[] = [];

  beforeAll(() => {
    class MockWebSocket {
      public onmessage: ((event: MessageEvent) => void) | null = null;

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
    fetchAgentsMock.mockReset();
    createAgentMock.mockReset();
    fetchAgentRunsMock.mockReset();
    runAgentMock.mockReset();
    fetchAgentRunsMock.mockResolvedValue([]);
    runAgentMock.mockResolvedValue(undefined);
  });

  afterEach(() => {
    cleanup();
    window.localStorage.clear();
  });

  function renderDashboard(initialAgents: AgentSummary[], runsByAgent?: Record<number, AgentRun[]>) {
    fetchAgentsMock.mockResolvedValue(initialAgents);
    const runsLookup = runsByAgent ?? {};
    fetchAgentRunsMock.mockImplementation(async (agentId: number) => runsLookup[agentId] ?? []);

    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false, cacheTime: 0 },
      },
    });

    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <DashboardPage />
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  test("renders DOM identical to legacy fixture", async () => {
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

    const container = document.getElementById("dashboard-container");
    expect(container).not.toBeNull();

    const rendered = normalizeMarkup(container?.innerHTML ?? "");
    const fixturePath = path.resolve(__dirname, "__fixtures__/legacy-dashboard.html");
    const fixture = normalizeMarkup(readFileSync(fixturePath, "utf-8"));

    expect(rendered).toEqual(fixture);
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

    await waitFor(() => expect(fetchAgentRunsMock).toHaveBeenCalledWith(1, 50));

    await screen.findByText("Show all (6)");
    const tables = screen.getAllByRole("table");
    expect(tables.length).toBeGreaterThan(1);
    expect(within(tables[1]).getAllByText("✔").length).toBeGreaterThan(0);

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

    const statusHeader = document.querySelector<HTMLElement>('th[data-column="status"]');
    expect(statusHeader).not.toBeNull();
    if (!statusHeader) {
      throw new Error("Status header not found");
    }
    fireEvent.click(statusHeader);
    await waitFor(() => {
      expect(window.localStorage.getItem("dashboard_sort_key")).toBe("status");
    });

    await waitFor(() => {
      const rowOrder = Array.from(document.querySelectorAll<HTMLTableRowElement>("tr[data-agent-id]")).map((row) => row.getAttribute("data-agent-id")).slice(0, agents.length);
      expect(rowOrder).toEqual(["2", "1"]);
    });

    fireEvent.click(statusHeader);

    await waitFor(() => {
      const rowOrder = Array.from(document.querySelectorAll<HTMLTableRowElement>("tr[data-agent-id]")).map((row) => row.getAttribute("data-agent-id")).slice(0, agents.length);
      expect(rowOrder).toEqual(["1", "2"]);
    });
  });
});

function normalizeMarkup(html: string): string {
  return html
    .replace(/\s+/g, " ")
    .replace(/> </g, "><")
    .trim();
}
