import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes, useLocation } from "react-router-dom";
import * as reactRouterDom from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Runner } from "../../services/api";
import { TestRouter } from "../../test/test-utils";
import RunnersPage from "../RunnersPage";

const runnerHookMocks = vi.hoisted(() => ({
  useRunners: vi.fn(),
}));

vi.mock("../../hooks/useRunners", () => ({
  useRunners: runnerHookMocks.useRunners,
}));

vi.mock("../../lib/readiness-contract", () => ({
  useReadinessFlag: vi.fn(),
}));

const { useRunners: mockUseRunners } = runnerHookMocks;

function makeRunner(overrides: Partial<Runner> = {}): Runner {
  const now = "2026-03-21T12:00:00Z";
  return {
    id: 1,
    owner_id: 1,
    name: "cube",
    availability_policy: "always_on",
    labels: null,
    capabilities: ["exec.full"],
    status: "online",
    status_reason: null,
    status_summary: "Ready to launch Longhouse sessions.",
    last_seen_at: now,
    last_seen_age_seconds: 3,
    heartbeat_interval_ms: 30_000,
    stale_after_seconds: 90,
    runner_metadata: { hostname: "cube" },
    install_mode: "native",
    auto_update_policy: "notify",
    install_layout_version: 1,
    managed_install_ready: true,
    runner_version: "1.0.0",
    latest_runner_version: "1.0.0",
    version_status: "current",
    reported_capabilities: ["exec.full"],
    capabilities_match: true,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-probe">{location.pathname}</div>;
}

function renderRunnersPage(initialEntry = "/runners", queryClient = createQueryClient()) {
  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route
            path="/runners"
            element={
              <>
                <RunnersPage />
                <LocationProbe />
              </>
            }
          />
          <Route
            path="/runners/:id"
            element={
              <>
                <div>Runner Detail</div>
                <LocationProbe />
              </>
            }
          />
        </Routes>
      </TestRouter>
    </QueryClientProvider>
  );
}

describe("RunnersPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseRunners.mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("opens the launch modal from a ready runner card without leaving the grid", async () => {
    const user = userEvent.setup();
    const navigateMock = vi.fn();
    vi.spyOn(reactRouterDom, "useNavigate").mockReturnValue(navigateMock);

    mockUseRunners.mockReturnValue({
      data: [makeRunner()],
      isLoading: false,
      error: null,
    });

    renderRunnersPage();

    await user.click(screen.getByRole("button", { name: "Start Longhouse Session" }));

    expect(screen.getByRole("dialog", { name: "Launch Longhouse session" })).toBeInTheDocument();
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/runners");
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("only shows launch actions for runners that are ready to host sessions", () => {
    mockUseRunners.mockReturnValue({
      data: [
        makeRunner({ id: 1, name: "cube" }),
        makeRunner({
          id: 2,
          name: "laptop",
          status: "offline",
          capabilities: ["exec.full"],
        }),
      ],
      isLoading: false,
      error: null,
    });

    renderRunnersPage();

    expect(screen.getAllByRole("button", { name: "Start Longhouse Session" })).toHaveLength(1);
    expect(screen.getByText("laptop")).toBeInTheDocument();
  });

  it("still navigates to runner detail when the card itself is selected", async () => {
    const user = userEvent.setup();
    const navigateMock = vi.fn();
    vi.spyOn(reactRouterDom, "useNavigate").mockReturnValue(navigateMock);

    mockUseRunners.mockReturnValue({
      data: [makeRunner()],
      isLoading: false,
      error: null,
    });

    renderRunnersPage();
    const runnerCard = document.querySelector(".runner-card");
    expect(runnerCard).not.toBeNull();

    await user.click(runnerCard as HTMLElement);

    expect(navigateMock).toHaveBeenCalledWith("/runners/1");
  });
});
