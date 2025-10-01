import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AdminPage from "../AdminPage";

// Mock the auth hook with admin user
const mockAdminUser = {
  id: 1,
  email: "admin@example.com",
  display_name: "Admin User",
  is_active: true,
  created_at: "2024-01-01T00:00:00Z",
  role: "ADMIN",
};

const mockAuth = {
  user: mockAdminUser,
  isAuthenticated: true,
  isLoading: false,
  login: vi.fn(),
  logout: vi.fn(),
  getToken: vi.fn(() => "mock-admin-token"),
};

vi.mock("../../lib/auth", () => ({
  useAuth: () => mockAuth,
}));

// Mock fetch for API calls
const mockFetch = vi.fn();
global.fetch = mockFetch;

const mockOpsSummary = {
  runs_today: 45,
  errors_today: 2,
  cost_today: 1.23,
  runs_7d: 312,
  errors_7d: 15,
  cost_7d: 8.95,
  runs_30d: 1250,
  errors_30d: 67,
  cost_30d: 34.56,
  budget_used: 68.5,
  budget_limit: 50.0,
};

const mockTopAgents = {
  top_agents: [
    {
      agent_id: 1,
      agent_name: "Test Agent",
      total_runs: 50,
      success_rate: 94.2,
      avg_cost: 0.125,
    },
    {
      agent_id: 2,
      agent_name: "Helper Agent",
      total_runs: 30,
      success_rate: 86.7,
      avg_cost: 0.089,
    },
  ],
};

function renderAdminPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <AdminPage />
    </QueryClientProvider>
  );
}

describe("AdminPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    // Mock successful API responses
    mockFetch.mockImplementation((url: string) => {
      if (url.includes("/api/ops/summary")) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(mockOpsSummary),
        });
      }
      if (url.includes("/api/ops/top")) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(mockTopAgents),
        });
      }
      return Promise.resolve({
        ok: false,
        text: () => Promise.resolve("Not found"),
      });
    });
  });

  it("renders operations dashboard", async () => {
    renderAdminPage();

    expect(screen.getByText("Operations Dashboard")).toBeInTheDocument();
    expect(screen.getByText("Time Window:")).toBeInTheDocument();
  });

  it("displays ops summary metrics", async () => {
    renderAdminPage();

    // Wait for data to load
    await waitFor(() => {
      expect(screen.getByText("45")).toBeInTheDocument(); // runs_today
    });

    expect(screen.getByText("2")).toBeInTheDocument(); // errors_today
    expect(screen.getByText("$1.2300")).toBeInTheDocument(); // cost_today
    expect(screen.getByText("68.5%")).toBeInTheDocument(); // budget_used
  });

  it("displays top agents table", async () => {
    renderAdminPage();

    await waitFor(() => {
      expect(screen.getByText("Test Agent")).toBeInTheDocument();
    });

    expect(screen.getByText("Helper Agent")).toBeInTheDocument();
    expect(screen.getByText("94.2%")).toBeInTheDocument(); // success rate
    expect(screen.getByText("$0.1250")).toBeInTheDocument(); // avg cost
  });

  it("allows changing time window", async () => {
    renderAdminPage();
    const user = userEvent.setup();

    // Wait for initial data load
    await waitFor(() => {
      expect(screen.getByText("45")).toBeInTheDocument(); // today runs
    });

    const select = screen.getByRole("combobox");
    await user.selectOptions(select, "7d");

    await waitFor(() => {
      // Should now show 7-day metrics
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining("window=7d"),
        expect.any(Object)
      );
    });
  });

  it("handles API errors gracefully", async () => {
    // Mock API failure
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 403,
      text: () => Promise.resolve("Admin access required"),
    });

    renderAdminPage();

    await waitFor(() => {
      expect(screen.getByText("Failed to load operations data")).toBeInTheDocument();
    });

    expect(screen.getByText("Retry")).toBeInTheDocument();
  });

  it("shows loading states", async () => {
    // Mock delayed response
    let resolvePromise: (value: any) => void;
    const promise = new Promise((resolve) => {
      resolvePromise = resolve;
    });

    mockFetch.mockImplementation((url: string) => {
      if (url.includes("/api/ops/summary")) {
        return promise;
      }
      return Promise.resolve({ ok: false, text: () => Promise.resolve("") });
    });

    renderAdminPage();

    expect(screen.getByText("Loading operations data...")).toBeInTheDocument();

    // Resolve the promise
    resolvePromise!({
      ok: true,
      json: () => Promise.resolve(mockOpsSummary),
    });

    await waitFor(() => {
      expect(screen.getByText("45")).toBeInTheDocument();
    });
  });
});