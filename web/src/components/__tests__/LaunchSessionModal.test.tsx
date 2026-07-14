import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, beforeEach, vi } from "vitest";
import LaunchSessionModal from "../LaunchSessionModal";
import type { MachineDirectoryEntry } from "../../services/api";

const apiMocks = vi.hoisted(() => ({
  fetchWorkspaceSuggestions: vi.fn(),
  listMachines: vi.fn(),
  launchRemoteSession: vi.fn(),
}));

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return {
    ...actual,
    ...apiMocks,
  };
});

function renderModal(props: Partial<React.ComponentProps<typeof LaunchSessionModal>> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const defaultProps: React.ComponentProps<typeof LaunchSessionModal> = {
    isOpen: true,
    onClose: vi.fn(),
    onLaunched: vi.fn(),
    ...props,
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <LaunchSessionModal {...defaultProps} />
    </QueryClientProvider>,
  );
}

function machine(overrides: Partial<MachineDirectoryEntry> = {}): MachineDirectoryEntry {
  const online = overrides.online ?? true;
  const canLaunch = overrides.can_launch_codex ?? online;
  const controlChannelStatus: MachineDirectoryEntry["control_channel_status"] = online ? "connected" : "disconnected";
  const launchBlockedBy: MachineDirectoryEntry["launch_blocked_by"] =
    overrides.launch_blocked_by ?? (canLaunch ? null : online ? "no_codex_support" : "control_down");
  const operations = overrides.control_operations_by_provider ?? (canLaunch ? { codex: ["launch", "run_once"] } : {});
  const providerOptions = Object.entries(operations)
    .map(([provider, providerOperations]) => ({
      provider,
      execution_lifetimes: [
        ...(providerOperations.includes("run_once") ? (["one_shot"] as const) : []),
        ...(providerOperations.includes("launch") ? (["live_control"] as const) : []),
      ],
    }))
    .filter((option) => option.execution_lifetimes.length > 0);
  const defaultExecutionLifetime = providerOptions.some((option) => option.execution_lifetimes.includes("one_shot"))
    ? "one_shot"
    : providerOptions.length > 0
      ? "live_control"
      : null;
  const defaultCandidates = providerOptions.filter((option) =>
    defaultExecutionLifetime ? option.execution_lifetimes.includes(defaultExecutionLifetime) : false,
  );
  return {
    device_id: "cinder",
    machine_name: "cinder",
    online,
    control_channel_status: controlChannelStatus,
    supports: canLaunch ? ["codex.launch", "codex.run_once"] : [],
    control_operations_by_provider: operations,
    can_launch_codex: canLaunch,
    launchable_providers: canLaunch ? ["codex"] : [],
    launch_blocked_by: launchBlockedBy,
    last_seen_at: null,
    engine_build: null,
    launch: overrides.launch ?? {
      blocked_by: providerOptions.length > 0 ? null : launchBlockedBy,
      providers: providerOptions,
      default_provider:
        defaultCandidates.find((option) => option.provider === "codex")?.provider ??
        defaultCandidates[0]?.provider ??
        null,
      default_execution_lifetime: defaultExecutionLifetime,
    },
    ...overrides,
  };
}

describe("LaunchSessionModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.fetchWorkspaceSuggestions.mockResolvedValue({
      device_id: "cinder",
      workspaces: [],
    });
  });

  it("shows an empty state when no machines are enrolled", async () => {
    apiMocks.listMachines.mockResolvedValue({ machines: [] });

    renderModal();

    expect(await screen.findByTestId("launch-no-machines")).toBeInTheDocument();
  });

  it("shows offline-only state when no launchable machines are available", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({
          device_id: "cinder",
          machine_name: "cinder",
          online: false,
          control_channel_status: "disconnected",
          can_launch_codex: false,
          launch_blocked_by: "control_down",
        }),
        machine({
          device_id: "old-ci-1",
          machine_name: "old-ci-1",
          online: false,
          control_channel_status: "disconnected",
          can_launch_codex: false,
          launch_blocked_by: "control_down",
        }),
        machine({
          device_id: "old-ci-2",
          machine_name: "old-ci-2",
          online: false,
          control_channel_status: "disconnected",
          can_launch_codex: false,
          launch_blocked_by: "control_down",
        }),
        machine({
          device_id: "old-ci-3",
          machine_name: "old-ci-3",
          online: false,
          control_channel_status: "disconnected",
          can_launch_codex: false,
          launch_blocked_by: "control_down",
        }),
      ],
    });
    renderModal();
    expect(await screen.findByTestId("launch-no-launchable")).toBeInTheDocument();
    expect(
      screen.getByText(/4 enrolled machines have no active control channel: cinder, old-ci-1, old-ci-2, plus 1 more\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/This sheet refreshes automatically\./)).toBeInTheDocument();
    expect(screen.queryByText(/does not advertise codex\.launch/)).not.toBeInTheDocument();
  });

  it("shows connected machines blocked by missing Codex launch support", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({
          device_id: "old-engine",
          machine_name: "old-engine",
          online: true,
          supports: ["codex.send"],
          can_launch_codex: false,
          launch_blocked_by: "no_codex_support",
        }),
      ],
    });
    renderModal();
    expect(await screen.findByTestId("launch-no-launchable")).toBeInTheDocument();
    expect(screen.getByText(/old-engine/)).toBeInTheDocument();
    expect(screen.getByText(/connected, but this engine does not advertise Codex launch/)).toBeInTheDocument();
  });

  it("shows connected machines with unproven provider operations as not launchable", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({
          device_id: "antigravity-host",
          machine_name: "antigravity-host",
          online: true,
          supports: ["antigravity.send"],
          control_operations_by_provider: {},
          can_launch_codex: false,
          launchable_providers: [],
          launch_blocked_by: "no_launch_support",
        }),
      ],
    });
    renderModal();
    expect(await screen.findByTestId("launch-no-launchable")).toBeInTheDocument();
    expect(screen.getByText(/antigravity-host/)).toBeInTheDocument();
    expect(screen.getByText(/connected, but this engine cannot remote-launch provider sessions/)).toBeInTheDocument();
  });

  it("dismisses on Escape", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({
          device_id: "cinder",
          machine_name: "cinder",
          online: true,
        }),
      ],
    });
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderModal({ onClose });
    await screen.findByTestId("launch-cwd-input");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("keeps offline machines visible below ready machines without build hashes", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({ device_id: "cinder", machine_name: "cinder", engine_build: "abc123" }),
        machine({
          device_id: "cube",
          machine_name: "cube",
          online: false,
          control_channel_status: "disconnected",
          can_launch_codex: false,
          launch_blocked_by: "control_down",
        }),
      ],
    });

    renderModal();

    const picker = await screen.findByTestId("launch-machine-select");
    expect(picker).toHaveTextContent("Available");
    expect(picker).toHaveTextContent("cinder");
    expect(picker).toHaveTextContent("Ready");
    expect(picker).toHaveTextContent("Unavailable");
    expect(picker).toHaveTextContent("cube");
    expect(picker).toHaveTextContent("control channel disconnected");
    expect(picker).not.toHaveTextContent("abc123");
  });

  it("submits a launch and invokes onLaunched with the session id", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({
          device_id: "cinder",
          machine_name: "cinder",
          online: true,
          control_channel_status: "connected",
          supports: ["codex.launch", "codex.run_once"],
          control_operations_by_provider: { codex: ["launch", "run_once"] },
          can_launch_codex: true,
          launch_blocked_by: null,
          last_seen_at: "2026-05-12T13:00:00Z",
          engine_build: "abc",
        }),
      ],
    });
    apiMocks.launchRemoteSession.mockResolvedValue({
      session_id: "new-session-id",
      launch_state: "live",
      execution_lifetime: "one_shot",
      launch_error_code: null,
      launch_error_message: null,
    });

    const onLaunched = vi.fn();
    const user = userEvent.setup();
    renderModal({ onLaunched });

    const cwdInput = await screen.findByTestId("launch-cwd-input");
    await user.type(cwdInput, "/Users/me/repo");
    expect(screen.getByTestId("launch-advanced-runtime")).not.toHaveAttribute("open");
    expect(screen.getByTestId("launch-submit")).toBeDisabled();
    await user.type(screen.getByTestId("launch-initial-prompt"), "Fix the telemetry test");
    await user.click(screen.getByTestId("launch-submit"));

    await waitFor(() => expect(onLaunched).toHaveBeenCalledWith("new-session-id"));
    expect(apiMocks.launchRemoteSession).toHaveBeenCalledWith(
      expect.objectContaining({
        device_id: "cinder",
        provider: "codex",
        cwd: "/Users/me/repo",
        initial_prompt: "Fix the telemetry test",
        execution_lifetime: "one_shot",
      }),
    );
  });

  it("falls back to live-control launch when run-once is not advertised", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({
          device_id: "old-cinder",
          machine_name: "old-cinder",
          supports: ["codex.launch"],
          control_operations_by_provider: { codex: ["launch"] },
          launchable_providers: ["codex"],
        }),
      ],
    });
    apiMocks.launchRemoteSession.mockResolvedValue({
      session_id: "live-session-id",
      launch_state: "live",
      execution_lifetime: "live_control",
      launch_error_code: null,
      launch_error_message: null,
    });

    const user = userEvent.setup();
    renderModal();

    await user.type(await screen.findByTestId("launch-cwd-input"), "/Users/me/repo");
    expect(screen.queryByTestId("launch-initial-prompt")).not.toBeInTheDocument();
    await user.click(screen.getByText("Advanced"));
    expect(screen.getByRole("button", { name: "Keep runtime open" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByTestId("launch-submit"));

    await waitFor(() => expect(apiMocks.launchRemoteSession).toHaveBeenCalled());
    expect(apiMocks.launchRemoteSession).toHaveBeenCalledWith(
      expect.objectContaining({
        device_id: "old-cinder",
        provider: "codex",
        cwd: "/Users/me/repo",
        execution_lifetime: "live_control",
      }),
    );
    expect(apiMocks.launchRemoteSession.mock.calls[0][0]).not.toHaveProperty("initial_prompt");
  });

  it("keeps live-control launch behind the advanced runtime picker", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({
          device_id: "cinder",
          machine_name: "cinder",
          online: true,
          control_channel_status: "connected",
          supports: ["codex.launch", "codex.run_once"],
          control_operations_by_provider: { codex: ["launch", "run_once"] },
          can_launch_codex: true,
          launch_blocked_by: null,
        }),
      ],
    });
    apiMocks.launchRemoteSession.mockResolvedValue({
      session_id: "live-session-id",
      launch_state: "live",
      execution_lifetime: "live_control",
      launch_error_code: null,
      launch_error_message: null,
    });

    const user = userEvent.setup();
    renderModal();

    await user.type(await screen.findByTestId("launch-cwd-input"), "/Users/me/repo");
    await user.click(screen.getByText("Advanced"));
    await user.click(screen.getByRole("button", { name: "Keep runtime open" }));
    expect(screen.queryByTestId("launch-initial-prompt")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("launch-submit"));

    await waitFor(() => expect(apiMocks.launchRemoteSession).toHaveBeenCalled());
    expect(apiMocks.launchRemoteSession).toHaveBeenCalledWith(
      expect.objectContaining({
        device_id: "cinder",
        provider: "codex",
        cwd: "/Users/me/repo",
        execution_lifetime: "live_control",
      }),
    );
  });

  it("prefills the top-ranked workspace and lets you pick another by label", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [machine({ device_id: "cinder", machine_name: "cinder", online: true })],
    });
    apiMocks.fetchWorkspaceSuggestions.mockResolvedValue({
      device_id: "cinder",
      workspaces: [
        {
          path: "/Users/example/git/zerg",
          label: "zerg (main)",
          git_repo: "git@github.com:cipher982/zerg.git",
          git_branch: "main",
          score: 320,
          last_used_at: "2026-06-03T00:00:00Z",
          session_count: 33,
        },
        {
          path: "/Users/example",
          label: "~",
          git_repo: null,
          git_branch: null,
          score: 90,
          last_used_at: "2026-06-02T00:00:00Z",
          session_count: 4,
        },
      ],
    });

    const user = userEvent.setup();
    renderModal();

    const cwdInput = await screen.findByTestId("launch-cwd-input");
    await waitFor(() => expect(cwdInput).toHaveValue("/Users/example/git/zerg"));

    await user.click(screen.getByRole("button", { name: "~" }));
    expect(cwdInput).toHaveValue("/Users/example");
  });

  it("keeps immediate launch failures in the modal instead of navigating", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({
          device_id: "cinder",
          machine_name: "cinder",
          online: true,
          can_launch_codex: true,
          launch_blocked_by: null,
        }),
      ],
    });
    apiMocks.launchRemoteSession.mockResolvedValue({
      session_id: "failed-session-id",
      launch_state: "launch_failed",
      execution_lifetime: "one_shot",
      launch_error_code: "cwd_not_allowed",
      launch_error_message: "Check the workspace path: cwd must be absolute",
    });

    const onLaunched = vi.fn();
    const user = userEvent.setup();
    renderModal({ onLaunched });

    const cwdInput = await screen.findByTestId("launch-cwd-input");
    await user.type(cwdInput, "/Users/example/git/zerg");
    await user.type(screen.getByTestId("launch-initial-prompt"), "Start and fail");
    await user.click(screen.getByTestId("launch-submit"));

    expect(await screen.findByTestId("launch-error")).toHaveTextContent(
      "Check the workspace path: cwd must be absolute",
    );
    expect(onLaunched).not.toHaveBeenCalled();
  });
});
