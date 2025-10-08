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
    mockFetch.mockImplementation((url: string) => {
      if (url.includes("/api/ops/summary")) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(mockOpsSummary),
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

    expect(screen.getByText("2")).toBeInTheDocument(); // errors_last_hour
    expect(screen.getByText("$1.2300")).toBeInTheDocument(); // cost_today_usd
    expect(screen.getByText("24.6%")).toBeInTheDocument(); // budget_user.percent
  });

  it("displays top agents table", async () => {
    renderAdminPage();

    await waitFor(() => {
      expect(screen.getByText("Test Agent")).toBeInTheDocument();
    });

    expect(screen.getByText("Helper Agent")).toBeInTheDocument();
    expect(screen.getByText("test@example.com")).toBeInTheDocument(); // owner_email
    expect(screen.getByText("$0.1250")).toBeInTheDocument(); // cost_usd
    expect(screen.getByText("300ms")).toBeInTheDocument(); // p95_ms
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