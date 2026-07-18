import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, beforeEach, vi } from "vitest";
import LaunchSessionModal from "../LaunchSessionModal";
import type { MachineDirectoryEntry } from "../../services/api";

const apiMocks = vi.hoisted(() => ({
  createConsoleSession: vi.fn(),
  fetchWorkspaceSuggestions: vi.fn(),
  listMachines: vi.fn(),
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
  const operations = overrides.control_operations_by_provider ?? (canLaunch ? { codex: ["turn_start"] } : {});
  const providerOptions = Object.entries(operations)
    .filter(([, providerOperations]) => providerOperations.includes("turn_start"))
    .map(([provider]) => ({ provider }));
  return {
    device_id: "cinder",
    machine_name: "cinder",
    online,
    control_channel_status: controlChannelStatus,
    supports: canLaunch ? ["codex.turn_start"] : [],
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
        providerOptions.find((option) => option.provider === "codex")?.provider ??
        providerOptions[0]?.provider ??
        null,
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
    expect(screen.getByText("No machines ready to launch")).toBeInTheDocument();
    expect(screen.getByText("old-ci-3")).toBeInTheDocument();
    expect(screen.getAllByText("Offline")).toHaveLength(4);
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
    expect(screen.getByText("Console launch unavailable")).toBeInTheDocument();
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
    expect(screen.getByText("Console launch unavailable")).toBeInTheDocument();
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

  it("supports keyboard navigation and closes the machine chooser before the modal", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({ device_id: "cinder", machine_name: "cinder" }),
        machine({ device_id: "cube", machine_name: "cube" }),
      ],
    });
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderModal({ onClose });

    const picker = await screen.findByTestId("launch-machine-select");
    await user.click(picker.querySelector("summary")!);
    const cinder = screen.getByRole("option", { name: /cinder Ready/ });
    const cube = screen.getByRole("option", { name: /cube Ready/ });
    cinder.focus();
    await user.keyboard("{ArrowDown}");
    expect(cube).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(picker).not.toHaveAttribute("open");
    expect(onClose).not.toHaveBeenCalled();
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
    expect(picker).toHaveTextContent("Offline");
    expect(picker).not.toHaveTextContent("abc123");
  });

  it("does not expose task or process-lifetime controls", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({ device_id: "cinder", machine_name: "cinder" }),
        machine({ device_id: "cube", machine_name: "cube" }),
      ],
    });
    const user = userEvent.setup();
    renderModal();

    await screen.findByTestId("launch-machine-select");
    await user.click(screen.getByRole("option", { name: /cube Ready/ }));

    expect(screen.queryByText("Task")).not.toBeInTheDocument();
    expect(screen.queryByText("Keep session open")).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: /cube Ready/ })).toHaveAttribute("aria-selected", "true");
  });

  it("creates an empty Console session and invokes onLaunched with the session id", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({
          device_id: "cinder",
          machine_name: "cinder",
          online: true,
          control_channel_status: "connected",
          supports: ["codex.launch", "codex.run_once"],
          control_operations_by_provider: { codex: ["turn_start"] },
          can_launch_codex: true,
          launch_blocked_by: null,
          last_seen_at: "2026-05-12T13:00:00Z",
          engine_build: "abc",
        }),
      ],
    });
    apiMocks.createConsoleSession.mockResolvedValue({
      session_id: "new-session-id",
      thread_id: "new-thread-id",
      created: true,
    });

    const onLaunched = vi.fn();
    const user = userEvent.setup();
    renderModal({ onLaunched });

    const cwdInput = await screen.findByTestId("launch-cwd-input");
    await user.type(cwdInput, "/Users/me/repo");
    expect(screen.getByTestId("launch-advanced-runtime")).not.toHaveAttribute("open");
    expect(screen.getByTestId("launch-submit")).toBeEnabled();
    await user.click(screen.getByTestId("launch-submit"));

    await waitFor(() => expect(onLaunched).toHaveBeenCalledWith("new-session-id"));
    expect(apiMocks.createConsoleSession).toHaveBeenCalledWith(
      expect.objectContaining({
        device_id: "cinder",
        provider: "codex",
        cwd: "/Users/me/repo",
        launch_surface: "web",
      }),
    );
  });

  it("launches OpenCode when the machine advertises turn-start parity", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        machine({
          control_operations_by_provider: {
            codex: ["turn_start"],
            opencode: ["turn_start", "turn_interrupt"],
          },
          launchable_providers: ["codex", "opencode"],
          supports: ["codex.turn_start", "opencode.turn_start", "opencode.turn_interrupt"],
          launch: {
            blocked_by: null,
            providers: [{ provider: "codex" }, { provider: "opencode" }],
            default_provider: "codex",
          },
        }),
      ],
    });
    apiMocks.createConsoleSession.mockResolvedValue({
      session_id: "opencode-session-id",
      thread_id: "opencode-thread-id",
      created: true,
    });
    const user = userEvent.setup();
    renderModal();

    const picker = await screen.findByTestId("launch-provider-select");
    await user.click(picker.querySelector("summary")!);
    await user.click(screen.getByRole("button", { name: "OpenCode" }));
    await user.type(screen.getByTestId("launch-cwd-input"), "/Users/me/opencode-project");
    await user.click(screen.getByTestId("launch-submit"));

    await waitFor(() =>
      expect(apiMocks.createConsoleSession).toHaveBeenCalledWith(
        expect.objectContaining({
          provider: "opencode",
          cwd: "/Users/me/opencode-project",
        }),
      ),
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

    await user.click(screen.getByRole("button", { name: /^~ / }));
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
    apiMocks.createConsoleSession.mockRejectedValue(new Error("Check the workspace path: cwd must be absolute"));

    const onLaunched = vi.fn();
    const user = userEvent.setup();
    renderModal({ onLaunched });

    const cwdInput = await screen.findByTestId("launch-cwd-input");
    await user.type(cwdInput, "/Users/example/git/zerg");
    await user.click(screen.getByTestId("launch-submit"));

    expect(await screen.findByTestId("launch-error")).toHaveTextContent("Launch failed");
    expect(onLaunched).not.toHaveBeenCalled();
  });
});
