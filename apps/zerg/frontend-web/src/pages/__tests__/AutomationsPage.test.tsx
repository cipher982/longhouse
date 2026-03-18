import React from "react";
import { describe, beforeAll, afterAll, beforeEach, afterEach, test, expect, vi } from "vitest";
import { render, screen, within, waitFor, fireEvent, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import AutomationsPage from "../AutomationsPage";
import { TestRouter } from "../../test/test-utils";
import { ConfirmProvider } from "../../components/confirm";
import {
  fetchAutomationOverview,
  runAutomation,
  updateAutomation,
  type AutomationSummary,
  type Run,
  type AutomationOverviewSnapshot,
} from "../../services/api";

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return {
    ...actual,
    fetchAutomationOverview: vi.fn(),
    runAutomation: vi.fn(),
    updateAutomation: vi.fn(),
  };
});

type MockWebSocketInstance = {
  onmessage: ((event: MessageEvent) => void) | null;
  close: () => void;
};

function buildAutomation(
  overrides: Partial<AutomationSummary> & Pick<AutomationSummary, "id" | "name" | "status" | "owner_id">
): AutomationSummary {
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

describe("AutomationsPage", () => {
  const fetchAutomationOverviewMock = fetchAutomationOverview as unknown as vi.MockedFunction<typeof fetchAutomationOverview>;
  const runAutomationMock = runAutomation as unknown as vi.MockedFunction<typeof runAutomation>;
  const updateAutomationMock = updateAutomation as unknown as vi.MockedFunction<typeof updateAutomation>;
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
    fetchAutomationOverviewMock.mockReset();
    runAutomationMock.mockReset();
    updateAutomationMock.mockReset();
    runAutomationMock.mockResolvedValue({ thread_id: 123 });
  });

  afterEach(() => {
    cleanup();
    window.localStorage.clear();
  });

  function renderAutomationsPage(initialAutomations: AutomationSummary[], runsByAutomation?: Record<number, Run[]>) {
    const runsLookup = runsByAutomation ?? {};
    const snapshot: AutomationOverviewSnapshot = {
      scope: "my",
      fetchedAt: new Date().toISOString(),
      runsLimit: 50,
      automations: initialAutomations,
      runs: initialAutomations.map((automation) => ({
        automationId: automation.id,
        runs: runsLookup[automation.id] ?? [],
      })),
    };

    fetchAutomationOverviewMock.mockResolvedValue(snapshot);

    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false, cacheTime: 0 },
      },
    });

    return render(
      <QueryClientProvider client={queryClient}>
        <ConfirmProvider>
          <TestRouter>
            <AutomationsPage />
          </TestRouter>
        </ConfirmProvider>
      </QueryClientProvider>
    );
  }

  test("renders automations header and automations table", async () => {
    const automations: AutomationSummary[] = [
      buildAutomation({
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
      buildAutomation({
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

    renderAutomationsPage(automations);

    await screen.findByText("Alpha");

    expect(screen.getByRole("button", { name: /Create Automation/i })).toBeInTheDocument();

    const allRows = screen.getAllByRole("row");
    const [headerRow, ...automationRows] = allRows;
    expect(within(headerRow).getByText("Name")).toBeInTheDocument();
    expect(within(headerRow).getByText("Status")).toBeInTheDocument();

    expect(automationRows).toHaveLength(2);
    expect(within(automationRows[0]).getByText("Alpha")).toBeInTheDocument();
    expect(within(automationRows[1]).getByText("Beta")).toBeInTheDocument();
  });

  test("expands an automation row and shows run history", async () => {
    const automation = buildAutomation({
      id: 1,
      name: "Runner",
      status: "idle",
      owner_id: 1,
      owner: null,
    });

    const runs: Run[] = [
      {
        id: 42,
        automation_id: 1,
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
        automation_id: 1,
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
        automation_id: 1,
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
        automation_id: 1,
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
        automation_id: 1,
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
        automation_id: 1,
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

    renderAutomationsPage([automation], { 1: runs });

    const row = await screen.findByRole("row", { name: /Runner/ });
    await userEvent.click(row);

    await waitFor(() => expect(fetchAutomationOverviewMock).toHaveBeenCalledTimes(1));

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

  test("sorts automations by status and toggles sort direction", async () => {
    const automations: AutomationSummary[] = [
      buildAutomation({ id: 1, name: "Alpha", status: "idle", owner_id: 1 }),
      buildAutomation({ id: 2, name: "Beta", status: "running", owner_id: 1 }),
    ];

    renderAutomationsPage(automations);

    const rows = await screen.findAllByRole("row");
    expect(rows[1]).toHaveTextContent("Alpha");

    const statusHeader = document.querySelector<HTMLElement>('[data-column="status"]');
    expect(statusHeader).not.toBeNull();
    if (!statusHeader) {
      throw new Error("Status header not found");
    }
    fireEvent.click(statusHeader);
    await waitFor(() => {
      expect(window.localStorage.getItem("automations_sort_key")).toBe("status");
    });

    await waitFor(() => {
      const rowOrder = Array.from(document.querySelectorAll<HTMLElement>('[data-automation-id]'))
        .map((row) => row.getAttribute("data-automation-id"))
        .slice(0, automations.length);
      expect(rowOrder).toEqual(["2", "1"]);
    });

    fireEvent.click(statusHeader);

    await waitFor(() => {
      const rowOrder = Array.from(document.querySelectorAll<HTMLElement>('[data-automation-id]'))
        .map((row) => row.getAttribute("data-automation-id"))
        .slice(0, automations.length);
      expect(rowOrder).toEqual(["1", "2"]);
    });
  });

  test("applies automation status updates from websocket events", async () => {
    const automation = buildAutomation({
      id: 42,
      name: "Speedy",
      status: "idle",
      owner_id: 9,
    });

    renderAutomationsPage([automation]);

    // Ensure automation row rendered
    await screen.findByText("Speedy");
    const socket = mockSockets[0];
    expect(socket).toBeDefined();

    // Simulate successful websocket connection
    socket.onopen?.(new Event("open"));

    // Wait for subscribe message to be sent
    await waitFor(() => {
      expect(socket.send).toHaveBeenCalledWith(expect.stringContaining("\"type\":\"subscribe\""));
    });

    const statusCell = document.querySelector<HTMLElement>('[data-automation-id="42"] [data-label="Status"]');
    expect(statusCell).not.toBeNull();
    if (!statusCell) {
      throw new Error("Status cell not found");
    }
    expect(statusCell.textContent).toContain("Idle");

    const payload = {
      type: "automation_updated",
      topic: "automation:42",
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
