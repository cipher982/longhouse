import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AdminPage from "../AdminPage";
import config from "../../lib/config";

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
  cost_today_usd: 1.23,
  budget_user: {
    limit_cents: 5000,
    used_usd: 1.23,
    percent: 24.6,
  },
  budget_global: {
    limit_cents: 10000,
    used_usd: 3.45,
    percent: 34.5,
  },
  active_users_24h: 5,
  agents_total: 12,
  agents_scheduled: 3,
  latency_ms: {
    p50: 250,
    p95: 800,
  },
  errors_last_hour: 2,
  top_agents_today: [
    {
      agent_id: 1,
      name: "Test Agent",
      owner_email: "test@example.com",
      runs: 50,
      cost_usd: 0.125,
      p95_ms: 300,
    },
    {
      agent_id: 2,
      name: "Helper Agent",
      owner_email: "helper@example.com",
      runs: 30,
      cost_usd: 0.089,
      p95_ms: 250,
    },
  ],
};

function renderAdminPage() {
  // Mock localStorage.zerg_jwt that AdminPage expects
  Object.defineProperty(window, 'localStorage', {
    value: {
      getItem: vi.fn((key: string) => {
        if (key === 'zerg_jwt') return 'mock-jwt-token';
        return null;
      }),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    },
    writable: true,
  });

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

    // Mock successful API responses - using real backend contract
    mockFetch.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();

      if (url.includes(`${config.apiBaseUrl}/ops/summary`)) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(mockOpsSummary),
        });
      }
      if (url.includes(`${config.apiBaseUrl}/admin/super-admin-status`)) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ is_super_admin: true, requires_password: false }),
        });
      }
      if (url.includes(`${config.apiBaseUrl}/admin/reset-database`)) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ message: "Reset triggered" }),
        });
      }
      // No separate /api/ops/top call - top agents included in summary
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

    const errorsHeading = screen.getAllByText("Errors (1h)")[0];
    const errorsCard = errorsHeading.closest(".metric-card");
    expect(errorsCard).not.toBeNull();
    expect(within(errorsCard as Element).getByText("2")).toBeInTheDocument();

    const costHeading = screen.getAllByText("Cost Today")[0];
    const costCard = costHeading.closest(".metric-card");
    expect(costCard).not.toBeNull();
    expect(within(costCard as Element).getByText("$1.2300")).toBeInTheDocument();

    const userBudgetHeading = screen.getAllByText("User Budget")[0];
    const userBudgetCard = userBudgetHeading.closest(".metric-card");
    expect(userBudgetCard).not.toBeNull();
    expect(within(userBudgetCard as Element).getByText("24.6%"))
      .toBeInTheDocument();
  });

  it("displays top agents table", async () => {
    renderAdminPage();

    await waitFor(() => {
      expect(screen.getAllByText("Test Agent").length).toBeGreaterThan(0);
    });

    expect(screen.getAllByText("Helper Agent").length).toBeGreaterThan(0);
    expect(screen.getAllByText("test@example.com").length).toBeGreaterThan(0); // owner_email
    expect(screen.getAllByText("$0.1250").length).toBeGreaterThan(0); // cost_usd
    expect(screen.getAllByText("300ms").length).toBeGreaterThan(0); // p95_ms
  });

  it("allows changing time window", async () => {
    renderAdminPage();
    const user = userEvent.setup();

    // Wait for initial data load
    await waitFor(() => {
      expect(screen.getAllByText("45").length).toBeGreaterThan(0); // today runs
    });

    const select = screen.getAllByRole("combobox")[0] as HTMLSelectElement;
    await user.selectOptions(select, "7d");

    expect(select.value).toBe("7d");
  });

  it("handles API errors gracefully", async () => {
    // Mock API failure (stable even if the query refires)
    mockFetch.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();

      if (url.includes(`${config.apiBaseUrl}/ops/summary`)) {
        return Promise.resolve({
          ok: false,
          status: 403,
          text: () => Promise.resolve("Admin access required"),
        } as unknown as Response);
      }

      // Keep other calls successful so the page can render the error state cleanly
      if (url.includes(`${config.apiBaseUrl}/admin/super-admin-status`)) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ is_super_admin: true, requires_password: false }),
        } as unknown as Response);
      }
      if (url.includes(`${config.apiBaseUrl}/admin/users`)) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ users: [], total: 0, limit: 100, offset: 0 }),
        } as unknown as Response);
      }

      return Promise.resolve({
        ok: false,
        text: () => Promise.resolve("Not found"),
      } as unknown as Response);
    });

    renderAdminPage();

    await waitFor(() => {
      // Admin now uses shared EmptyState copy
      expect(screen.getByText("Error loading operations")).toBeInTheDocument();
    });

    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("shows loading states", async () => {
    // Mock delayed response
    let resolvePromise: (value: any) => void;
    const promise = new Promise((resolve) => {
      resolvePromise = resolve;
    });

    mockFetch.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();

      if (url.includes(`${config.apiBaseUrl}/ops/summary`)) {
        return promise;
      }
      if (url.includes(`${config.apiBaseUrl}/admin/super-admin-status`)) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ is_super_admin: true, requires_password: false }),
        });
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
      expect(screen.getAllByText("45").length).toBeGreaterThan(0);
    });
  });
});
