import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes, useLocation } from "react-router";
import * as reactRouterDom from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MachineDirectoryEntry, Runner } from "@/shared/api/index";
import { TestRouter } from "@/shared/test/test-utils";
import RunnersPage from "../RunnersPage";

const runnerHookMocks = vi.hoisted(() => ({
  useRunners: vi.fn(),
}));

const machineApiMocks = vi.hoisted(() => ({
  listMachines: vi.fn(),
}));

vi.mock("@/shared/api/index", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/shared/api/index")>()),
  listMachines: machineApiMocks.listMachines,
}));

vi.mock("../useRunners", () => ({
  useRunners: runnerHookMocks.useRunners,
}));

vi.mock("@/shared/lib/readiness-contract", () => ({
  useReadinessFlag: vi.fn(),
}));

const { useRunners: mockUseRunners } = runnerHookMocks;

function makeRunner(overrides: Partial<Runner> = {}): Runner {
  const now = "2026-03-21T12:00:00Z";
  return {
    id: 1,
    owner_id: 1,
    name: "demo-machine",
    availability_policy: "always_on",
    labels: null,
    capabilities: ["exec.full"],
    status: "online",
    status_reason: null,
    status_summary: "Ready to start sessions.",
    last_seen_at: now,
    last_seen_age_seconds: 3,
    heartbeat_interval_ms: 30_000,
    stale_after_seconds: 90,
    runner_metadata: { hostname: "demo-machine" },
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

function makeMachine(overrides: Partial<MachineDirectoryEntry> = {}): MachineDirectoryEntry {
  return {
    device_id: "alex-macbook",
    machine_name: "alex-macbook",
    online: true,
    control_channel_status: "connected",
    last_seen_at: new Date().toISOString(),
    connected_since: new Date(Date.now() - 4 * 60_000).toISOString(),
    engine_build: "cd506ebf",
    provider_readiness: {},
    launch: { blocked_by: null, providers: [], default_provider: null, unavailable_providers: [] },
    ...overrides,
  } as MachineDirectoryEntry;
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
    machineApiMocks.listMachines.mockResolvedValue({ machines: [] });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lists a connected Machine Agent instead of saying there is nothing here", async () => {
    machineApiMocks.listMachines.mockResolvedValue({
      machines: [
        makeMachine({
          provider_readiness: { claude: { state: "not_authenticated", remediation: "Sign in to claude on this machine" } },
        }),
      ],
    });

    renderRunnersPage();

    const card = await screen.findByTestId("machine-agent-alex-macbook");
    expect(card).toHaveTextContent("alex-macbook");
    expect(card).toHaveTextContent("Online · connected 4 minutes ago");
    expect(card).toHaveTextContent("Agent cd506ebf");
    expect(card).toHaveTextContent("Sign in to claude on this machine to start sessions from Longhouse.");
    // The machine is here, so the page no longer claims nothing is connected.
    expect(screen.queryByText("No machines connected yet")).not.toBeInTheDocument();
    expect(screen.queryByText(/No Runners yet/)).not.toBeInTheDocument();
    expect(screen.getByTestId("runners-none")).toHaveTextContent("A Runner is an optional extra");
  });

  it("does not nag about a missing provider CLI while the machine can still start sessions", async () => {
    machineApiMocks.listMachines.mockResolvedValue({
      machines: [
        makeMachine({
          provider_readiness: {
            claude: { state: "ready" },
            codex: { state: "cli_missing", remediation: "Install codex on this machine" },
          },
          launch: { blocked_by: null, providers: [{ provider: "claude" }], default_provider: "claude", unavailable_providers: [] },
        }),
      ],
    });

    renderRunnersPage();

    const card = await screen.findByTestId("machine-agent-alex-macbook");
    expect(card).not.toHaveTextContent("Install codex");
    expect(card).not.toHaveTextContent("to start sessions from Longhouse");
  });

  it("shows an offline machine with when it was last seen", async () => {
    machineApiMocks.listMachines.mockResolvedValue({
      machines: [
        makeMachine({
          online: false,
          control_channel_status: "disconnected",
          connected_since: null,
          last_seen_at: new Date(Date.now() - 3 * 3_600_000).toISOString(),
        }),
      ],
    });

    renderRunnersPage();

    const card = await screen.findByTestId("machine-agent-alex-macbook");
    expect(card).toHaveAttribute("data-online", "false");
    expect(card).toHaveTextContent("Offline · Last seen 3 hours ago");
  });

  it("guides a first-time host to connect a machine, and only once the lookup has settled", async () => {
    renderRunnersPage();

    expect(await screen.findByText("No machines connected yet")).toBeInTheDocument();
    expect(screen.getByTestId("runners-add-first-button")).toBeInTheDocument();
    expect(screen.queryByTestId("machine-agents")).not.toBeInTheDocument();
  });

  it("reports a failed machine lookup instead of claiming the host is empty", async () => {
    machineApiMocks.listMachines.mockRejectedValue(new Error("boom"));

    renderRunnersPage();

    expect(await screen.findByText("Could not load connected machines")).toBeInTheDocument();
    expect(screen.queryByText("No machines connected yet")).not.toBeInTheDocument();
  });

  it("does not render inline launch actions on runner cards anymore", () => {
    mockUseRunners.mockReturnValue({
      data: [makeRunner()],
      isLoading: false,
      error: null,
    });

    renderRunnersPage();

    expect(screen.queryByRole("button", { name: "Start Session" })).not.toBeInTheDocument();
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/runners");
  });

  it("removes runner-card launch actions regardless of runner readiness", () => {
    mockUseRunners.mockReturnValue({
      data: [
        makeRunner({ id: 1, name: "demo-machine" }),
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

    expect(screen.queryByRole("button", { name: "Start Session" })).not.toBeInTheDocument();
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
