import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SwarmOpsPage from "../SwarmOpsPage";
import { request } from "../../services/api";
import { TestRouter } from "../../test/test-utils";

const mockNavigate = vi.fn();

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return {
    ...actual,
    request: vi.fn(),
  };
});

const requestMock = request as unknown as vi.MockedFunction<typeof request>;

function renderSwarmOps() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, cacheTime: 0 },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={["/runs"]}>
        <SwarmOpsPage />
      </TestRouter>
    </QueryClientProvider>
  );
}

describe("SwarmOpsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    if (typeof window !== "undefined") {
      window.sessionStorage.clear();
    }

    requestMock.mockImplementation((path: string) => {
      if (path === "/oikos/runs?limit=120") {
        return Promise.resolve([
          {
            id: 7,
            task_id: 101,
            fiche_id: 77,
            thread_id: 501,
            task_name: "Priority Inbox",
            fiche_name: "Legacy Fiche Name",
            status: "running",
            summary: "Need your input",
            signal: "Need your input",
            signal_source: "summary",
            created_at: "2026-03-17T12:05:00Z",
            updated_at: "2026-03-17T12:05:00Z",
            completed_at: null,
          },
        ]);
      }

      if (path === "/oikos/runs/7/events?limit=120") {
        return Promise.resolve({
          run_id: 7,
          events: [],
          total: 0,
        });
      }

      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });
  });

  it("prefers task aliases for display and thread navigation", async () => {
    const user = userEvent.setup();
    renderSwarmOps();

    await waitFor(() => {
      expect(screen.getAllByText("Priority Inbox").length).toBeGreaterThan(0);
    });

    expect(screen.queryByText("Legacy Fiche Name")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Open thread" }));

    expect(mockNavigate).toHaveBeenCalledWith("/fiche/101/thread/501");
  });
});
