import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import * as reactRouterDom from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Runner } from "../../services/api";
import { TestRouter } from "../../test/test-utils";
import RunnerDetailPage from "../RunnerDetailPage";

const hookMocks = vi.hoisted(() => ({
  useCreateEnrollToken: vi.fn(),
  useDeleteRunner: vi.fn(),
  useRunner: vi.fn(),
  useRunnerDoctor: vi.fn(),
  useRunnerJobs: vi.fn(),
  useRevokeRunner: vi.fn(),
  useRotateRunnerSecret: vi.fn(),
  useUpdateRunner: vi.fn(),
}));

const confirmMock = vi.hoisted(() => vi.fn().mockResolvedValue(true));

vi.mock("../../hooks/useRunners", () => ({
  useCreateEnrollToken: hookMocks.useCreateEnrollToken,
  useDeleteRunner: hookMocks.useDeleteRunner,
  useRunner: hookMocks.useRunner,
  useRunnerDoctor: hookMocks.useRunnerDoctor,
  useRunnerJobs: hookMocks.useRunnerJobs,
  useRevokeRunner: hookMocks.useRevokeRunner,
  useRotateRunnerSecret: hookMocks.useRotateRunnerSecret,
  useUpdateRunner: hookMocks.useUpdateRunner,
}));

vi.mock("../../components/confirm", () => ({
  useConfirm: () => confirmMock,
}));

function makeRunner(overrides: Partial<Runner> = {}): Runner {
  const now = "2026-04-15T12:00:00Z";
  return {
    id: 1,
    owner_id: 1,
    name: "demo-machine",
    availability_policy: "always_on",
    labels: null,
    capabilities: ["exec.full"],
    status: "offline",
    status_reason: "stale_heartbeat",
    status_summary: "Offline. Last heartbeat 10m ago.",
    last_seen_at: now,
    last_seen_age_seconds: 600,
    heartbeat_interval_ms: 30_000,
    stale_after_seconds: 90,
    runner_metadata: { hostname: "demo-machine", platform: "linux", arch: "x64" },
    install_mode: "server",
    auto_update_policy: "notify",
    install_layout_version: 1,
    managed_install_ready: true,
    runner_version: "0.1.7",
    latest_runner_version: "0.1.7",
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

function renderRunnerDetailPage() {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <TestRouter initialEntries={["/runners/1"]}>
        <Routes>
          <Route path="/runners/:id" element={<RunnerDetailPage />} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>,
  );
}

describe("RunnerDetailPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hookMocks.useRunner.mockReturnValue({
      data: makeRunner(),
      isLoading: false,
      error: null,
    });
    hookMocks.useRunnerJobs.mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    });
    hookMocks.useUpdateRunner.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    });
    hookMocks.useDeleteRunner.mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue(undefined),
      isPending: false,
    });
    hookMocks.useRevokeRunner.mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({ success: true }),
      isPending: false,
    });
    hookMocks.useRotateRunnerSecret.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    });
    hookMocks.useRunnerDoctor.mockReturnValue({
      mutateAsync: vi.fn(),
    });
    hookMocks.useCreateEnrollToken.mockReturnValue({
      data: null,
      mutateAsync: vi.fn(),
      isPending: false,
      error: null,
    });
  });

  it("shows Forget Machine for offline runners and deletes them on confirm", async () => {
    const user = userEvent.setup();
    const navigateMock = vi.fn();
    const deleteMutation = vi.fn().mockResolvedValue(undefined);
    vi.spyOn(reactRouterDom, "useNavigate").mockReturnValue(navigateMock);
    hookMocks.useDeleteRunner.mockReturnValue({
      mutateAsync: deleteMutation,
      isPending: false,
    });

    renderRunnerDetailPage();

    await user.click(screen.getByRole("button", { name: "Forget Machine" }));

    await waitFor(() => {
      expect(confirmMock).toHaveBeenCalled();
      expect(deleteMutation).toHaveBeenCalledWith(1);
      expect(navigateMock).toHaveBeenCalledWith("/runners");
    });
  });

  it("does not show Forget Machine for online runners", () => {
    hookMocks.useRunner.mockReturnValue({
      data: makeRunner({
        status: "online",
        status_reason: "fresh_heartbeat",
        status_summary: "Online. Heartbeats are current.",
        last_seen_age_seconds: 4,
      }),
      isLoading: false,
      error: null,
    });

    renderRunnerDetailPage();

    expect(screen.queryByRole("button", { name: "Forget Machine" })).not.toBeInTheDocument();
  });
});
