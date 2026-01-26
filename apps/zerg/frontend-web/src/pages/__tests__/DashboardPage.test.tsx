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
  runFiche,
  updateFiche,
  type FicheSummary,
  type Course,
  type DashboardSnapshot,
} from "../../services/api";

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return {
    ...actual,
    fetchDashboardSnapshot: vi.fn(),
    runFiche: vi.fn(),
    updateFiche: vi.fn(),
  };
});

type MockWebSocketInstance = {
  onmessage: ((event: MessageEvent) => void) | null;
  close: () => void;
};

function buildFiche(
  overrides: Partial<FicheSummary> & Pick<FicheSummary, "id" | "name" | "status" | "owner_id">
): FicheSummary {
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
    next_course_at: overrides.next_course_at ?? null,
    last_course_at: overrides.last_course_at ?? null,
  };
}

describe("DashboardPage", () => {
  const fetchDashboardSnapshotMock = fetchDashboardSnapshot as unknown as vi.MockedFunction<typeof fetchDashboardSnapshot>;
  const runFicheMock = runFiche as unknown as vi.MockedFunction<typeof runFiche>;
  const updateFicheMock = updateFiche as unknown as vi.MockedFunction<typeof updateFiche>;
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
    runFicheMock.mockReset();
    updateFicheMock.mockReset();
    runFicheMock.mockResolvedValue({ thread_id: 123 });
  });

  afterEach(() => {
    cleanup();
    window.localStorage.clear();
  });

  function renderDashboard(initialFiches: FicheSummary[], coursesByFiche?: Record<number, Course[]>) {
    const coursesLookup = coursesByFiche ?? {};
    const snapshot: DashboardSnapshot = {
      scope: "my",
      fetchedAt: new Date().toISOString(),
      coursesLimit: 50,
      fiches: initialFiches,
      courses: initialFiches.map((fiche) => ({
        ficheId: fiche.id,
        courses: coursesLookup[fiche.id] ?? [],
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

  test("renders dashboard header and fiches table", async () => {
    const fiches: FicheSummary[] = [
      buildFiche({
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
        last_course_at: "2025-09-24T10:00:00.000Z",
        next_course_at: "2025-09-24T12:00:00.000Z",
      }),
      buildFiche({
        id: 2,
        name: "Beta",
        status: "error",
        owner_id: 9,
        owner: null,
        last_course_at: null,
        next_course_at: null,
        last_error: "Failed to execute",
      }),
    ];

    renderDashboard(fiches);

    await screen.findByText("Alpha");

    expect(screen.getByRole("button", { name: /Create Fiche/i })).toBeInTheDocument();

    const allRows = screen.getAllByRole("row");
    const [headerRow, ...ficheRows] = allRows;
    expect(within(headerRow).getByText("Name")).toBeInTheDocument();
    expect(within(headerRow).getByText("Status")).toBeInTheDocument();

    expect(ficheRows).toHaveLength(2);
    expect(within(ficheRows[0]).getByText("Alpha")).toBeInTheDocument();
    expect(within(ficheRows[1]).getByText("Beta")).toBeInTheDocument();
  });

  test("expands a fiche row and shows course history", async () => {
    const fiche = buildFiche({
      id: 1,
      name: "Runner",
      status: "idle",
      owner_id: 1,
      owner: null,
    });

    const courses: Course[] = [
      {
        id: 42,
        fiche_id: 1,
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
        fiche_id: 1,
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
        fiche_id: 1,
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
        fiche_id: 1,
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
        fiche_id: 1,
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
        fiche_id: 1,
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

    renderDashboard([fiche], { 1: courses });

    const row = await screen.findByRole("row", { name: /Runner/ });
    await userEvent.click(row);

    await waitFor(() => expect(fetchDashboardSnapshotMock).toHaveBeenCalledTimes(1));

    await screen.findByText("Show all (6)");
    const tables = screen.getAllByRole("table");
    expect(tables.length).toBeGreaterThan(1);
    // Course history table uses SVG icons (CheckCircleIcon) instead of text
    // Query for table rows to verify course data is displayed
    const courseHistoryTable = tables[1];
    const rows = within(courseHistoryTable).getAllByRole("row");
    // Should have at least header row + some data rows (we have 6 courses in test data)
    expect(rows.length).toBeGreaterThan(1);

    await userEvent.click(screen.getByText("Show all (6)"));
    expect(screen.getByText("Show less")).toBeInTheDocument();
  });

  test("sorts fiches by status and toggles sort direction", async () => {
    const fiches: FicheSummary[] = [
      buildFiche({ id: 1, name: "Alpha", status: "idle", owner_id: 1 }),
      buildFiche({ id: 2, name: "Beta", status: "running", owner_id: 1 }),
    ];

    renderDashboard(fiches);

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
      const rowOrder = Array.from(document.querySelectorAll<HTMLElement>('[data-fiche-id]'))
        .map((row) => row.getAttribute("data-fiche-id"))
        .slice(0, fiches.length);
      expect(rowOrder).toEqual(["2", "1"]);
    });

    fireEvent.click(statusHeader);

    await waitFor(() => {
      const rowOrder = Array.from(document.querySelectorAll<HTMLElement>('[data-fiche-id]'))
        .map((row) => row.getAttribute("data-fiche-id"))
        .slice(0, fiches.length);
      expect(rowOrder).toEqual(["1", "2"]);
    });
  });

  test("applies fiche status updates from websocket events", async () => {
    const fiche = buildFiche({
      id: 42,
      name: "Speedy",
      status: "idle",
      owner_id: 9,
    });

    renderDashboard([fiche]);

    // Ensure fiche row rendered
    await screen.findByText("Speedy");
    const socket = mockSockets[0];
    expect(socket).toBeDefined();

    // Simulate successful websocket connection
    socket.onopen?.(new Event("open"));

    // Wait for subscribe message to be sent
    await waitFor(() => {
      expect(socket.send).toHaveBeenCalledWith(expect.stringContaining("\"type\":\"subscribe\""));
    });

    const statusCell = document.querySelector<HTMLElement>('[data-fiche-id="42"] [data-label="Status"]');
    expect(statusCell).not.toBeNull();
    if (!statusCell) {
      throw new Error("Status cell not found");
    }
    expect(statusCell.textContent).toContain("Idle");

    const payload = {
      type: "fiche_updated",
      topic: "fiche:42",
      data: {
        id: 42,
        status: "running",
        last_error: null,
        last_course_at: "2025-11-08T23:59:00.000Z",
        next_course_at: null,
      },
    };

    socket.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);

    await waitFor(() => {
      expect(statusCell.textContent).toContain("Running");
    });
  });
});
