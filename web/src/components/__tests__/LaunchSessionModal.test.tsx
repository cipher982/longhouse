import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, beforeEach, vi } from "vitest";
import LaunchSessionModal from "../LaunchSessionModal";

const apiMocks = vi.hoisted(() => ({
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

describe("LaunchSessionModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows an empty state when no machines are enrolled", async () => {
    apiMocks.listMachines.mockResolvedValue({ machines: [] });

    renderModal();

    expect(await screen.findByTestId("launch-no-machines")).toBeInTheDocument();
  });

  it("shows offline-only state when no launchable machines are available", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        {
          device_id: "cinder",
          machine_name: "cinder",
          online: false,
          supports: [],
          last_seen_at: null,
          engine_build: null,
        },
        {
          device_id: "old-ci-1",
          machine_name: "old-ci-1",
          online: false,
          supports: [],
          last_seen_at: null,
          engine_build: null,
        },
        {
          device_id: "old-ci-2",
          machine_name: "old-ci-2",
          online: false,
          supports: [],
          last_seen_at: null,
          engine_build: null,
        },
        {
          device_id: "old-ci-3",
          machine_name: "old-ci-3",
          online: false,
          supports: [],
          last_seen_at: null,
          engine_build: null,
        },
      ],
    });
    renderModal();
    expect(await screen.findByTestId("launch-no-launchable")).toBeInTheDocument();
    expect(
      screen.getByText(/4 enrolled machines are offline: cinder, old-ci-1, old-ci-2, plus 1 more\./),
    ).toBeInTheDocument();
    expect(screen.queryByText(/does not advertise codex\.launch/)).not.toBeInTheDocument();
  });

  it("dismisses on Escape", async () => {
    apiMocks.listMachines.mockResolvedValue({
      machines: [
        {
          device_id: "cinder",
          machine_name: "cinder",
          online: true,
          supports: ["codex.launch"],
          last_seen_at: null,
          engine_build: null,
        },
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
        {
          device_id: "cinder",
          machine_name: "cinder",
          online: true,
          supports: ["codex.launch"],
          last_seen_at: "2026-05-12T13:00:00Z",
          engine_build: "abc",
        },
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
});
