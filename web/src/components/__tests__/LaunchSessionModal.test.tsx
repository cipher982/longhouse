import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LaunchSessionModal from "../LaunchSessionModal";
import { TestRouter } from "../../test/test-utils";

const apiMocks = vi.hoisted(() => ({
  launchManagedLocalSession: vi.fn(),
}));

vi.mock("../../services/api/sessionChat", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api/sessionChat")>();
  return {
    ...actual,
    launchManagedLocalSession: apiMocks.launchManagedLocalSession,
  };
});

function renderModal() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter>
        <LaunchSessionModal
          isOpen
          onClose={vi.fn()}
          runner={{
            id: 7,
            name: "cinder",
          } as never}
        />
      </TestRouter>
    </QueryClientProvider>,
  );
}

describe("LaunchSessionModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows the managed launch profile after a successful tmux launch", async () => {
    const user = userEvent.setup();
    apiMocks.launchManagedLocalSession.mockResolvedValue({
      session_id: "sess-1",
      provider: "claude",
      provider_session_id: "provider-sess-1",
      execution_home: "managed_local",
      managed_transport: "tmux",
      loop_mode: "manual",
      source_runner_id: 7,
      source_runner_name: "cinder",
      managed_session_name: "lh-claude-managed-local",
      attach_command: "zsh -lc 'exec tmux -L longhouse-managed attach -t lh-claude-managed-local'",
      managed_launch_profile: {
        required_commands: ["claude"],
        exported_env_keys: [
          "LONGHOUSE_MANAGED_SESSION_ID",
          "LONGHOUSE_HOOK_URL",
          "LONGHOUSE_HOOK_TOKEN",
          "AWS_PROFILE",
        ],
        argv: [
          "claude",
          "--dangerously-skip-permissions",
          "--session-id",
          "<provider-session-id>",
          "-n",
          "Managed Local Proof",
        ],
      },
    });

    renderModal();

    await user.type(screen.getByLabelText(/working directory/i), "/Users/davidrose/git/zerg");
    await user.click(screen.getByRole("button", { name: "Launch" }));

    await screen.findByText("Session started on cinder");
    await waitFor(() =>
      expect(apiMocks.launchManagedLocalSession).toHaveBeenCalledWith(
        {
          runner_target: "runner:7",
          cwd: "/Users/davidrose/git/zerg",
          provider: "claude",
          project: null,
          display_name: null,
        },
        expect.any(Object),
      ),
    );
    expect(screen.getByTestId("launch-session-profile")).toHaveTextContent("Launch profile");
    expect(screen.getByTestId("launch-session-profile")).toHaveTextContent("Required commands: claude");
    expect(screen.getByTestId("launch-session-profile")).toHaveTextContent(
      "Exported env keys: LONGHOUSE_MANAGED_SESSION_ID, LONGHOUSE_HOOK_URL, LONGHOUSE_HOOK_TOKEN, AWS_PROFILE",
    );
    expect(screen.getByTestId("launch-session-profile-argv")).toHaveTextContent(
      'claude --dangerously-skip-permissions --session-id <provider-session-id> -n "Managed Local Proof"',
    );
  });

  it("hides launch-profile details when the transport does not return them", async () => {
    const user = userEvent.setup();
    apiMocks.launchManagedLocalSession.mockResolvedValue({
      session_id: "sess-2",
      provider: "claude",
      provider_session_id: "provider-sess-2",
      execution_home: "managed_local",
      managed_transport: "claude_channel_bridge",
      loop_mode: "manual",
      source_runner_id: 7,
      source_runner_name: "cinder",
      managed_session_name: "lh-claude-managed-local",
      attach_command: "zsh -lc 'exec claude --resume provider-sess-2'",
      managed_launch_profile: null,
    });

    renderModal();

    await user.type(screen.getByLabelText(/working directory/i), "/Users/davidrose/git/zerg");
    await user.click(screen.getByRole("button", { name: "Launch" }));

    await screen.findByText("Session started on cinder");
    expect(screen.queryByTestId("launch-session-profile")).not.toBeInTheDocument();
  });
});
