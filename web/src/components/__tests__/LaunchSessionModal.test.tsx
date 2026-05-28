import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, beforeEach, vi } from "vitest";
import LaunchSessionModal from "../LaunchSessionModal";
import type { MachineDirectoryEntry } from "../../services/api";

const apiMocks = vi.hoisted(() => ({
  fetchAgentSessions: vi.fn(),
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
  const launchBlockedBy: MachineDirectoryEntry["launch_blocked_by"] = canLaunch
    ? null
    : online
      ? "no_codex_support"
      : "control_down";
  return {
    device_id: "cinder",
    machine_name: "cinder",
    online,
    control_channel_status: controlChannelStatus,
    supports: canLaunch ? ["codex.launch"] : [],
    control_operations_by_provider: canLaunch ? { codex: ["launch"] } : {},
    can_launch_codex: canLaunch,
    launchable_providers: canLaunch ? ["codex"] : [],
    launch_blocked_by: launchBlockedBy,
    last_seen_at: null,
    engine_build: null,
    ...overrides,
  };
}

describe("LaunchSessionModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.fetchAgentSessions.mockResolvedValue({
      sessions: [],
      total: 0,
      has_real_sessions: false,
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

  it("submits a launch and invokes onLaunched with the session id", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({
          device_id: "cinder",
          machine_name: "cinder",
          online: true,
          control_channel_status: "connected",
          supports: ["codex.launch"],
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
      launch_error_code: null,
      launch_error_message: null,
    });

    const onLaunched = vi.fn();
    const user = userEvent.setup();
    renderModal({ onLaunched });

    const cwdInput = await screen.findByTestId("launch-cwd-input");
    await user.type(cwdInput, "/Users/me/repo");
    await user.click(screen.getByTestId("launch-submit"));

    await waitFor(() => expect(onLaunched).toHaveBeenCalledWith("new-session-id"));
    expect(apiMocks.launchRemoteSession).toHaveBeenCalledWith(
      expect.objectContaining({
        device_id: "cinder",
        provider: "codex",
        cwd: "/Users/me/repo",
      }),
    );
  });

  it("prefills recent machine paths and offers a parent directory shortcut", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [machine({ device_id: "cinder", machine_name: "cinder", online: true })],
    });
    apiMocks.fetchAgentSessions.mockResolvedValue({
      sessions: [
        {
          head: { cwd: "/Users/davidrose/git/zerg/longhouse" },
          detail: { cwd: "/Users/davidrose/git/zerg/longhouse" },
          root: { cwd: "/Users/davidrose/git/zerg/longhouse" },
        },
      ],
      total: 1,
      has_real_sessions: true,
    } as any);

    const user = userEvent.setup();
    renderModal();

    const cwdInput = await screen.findByTestId("launch-cwd-input");
    await waitFor(() => expect(cwdInput).toHaveValue("/Users/davidrose/git/zerg/longhouse"));

    await user.click(screen.getByRole("button", { name: "~/git/zerg" }));
    expect(cwdInput).toHaveValue("/Users/davidrose/git/zerg");
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
      launch_error_code: "cwd_not_allowed",
      launch_error_message: "Check the workspace path: cwd must be absolute",
    });

    const onLaunched = vi.fn();
    const user = userEvent.setup();
    renderModal({ onLaunched });

    const cwdInput = await screen.findByTestId("launch-cwd-input");
    await user.type(cwdInput, "/Users/davidrose/git/zerg");
    await user.click(screen.getByTestId("launch-submit"));

    expect(await screen.findByTestId("launch-error")).toHaveTextContent(
      "Check the workspace path: cwd must be absolute",
    );
    expect(onLaunched).not.toHaveBeenCalled();
  });
});
