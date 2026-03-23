import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AutomationSettingsDrawer } from "../AutomationSettingsDrawer";

const hookState = vi.hoisted(() => ({
  automation: {
    id: 42,
    owner_id: 7,
    name: "Test automation",
    allowed_tools: ["container_exec"],
  },
  debouncedUpdateAllowedTools: {
    mutate: vi.fn(),
    flush: vi.fn(),
    cancelPending: vi.fn(),
    isPending: false,
    isError: false,
    hasPendingDebounce: false,
    error: null,
    lastSyncedValue: null,
  },
}))

vi.mock("../../confirm", () => ({
  useConfirm: () => vi.fn().mockResolvedValue(true),
}))

vi.mock("../../../hooks/useAutomationConfig", () => ({
  useAutomationDetails: () => ({ data: hookState.automation }),
  useContainerPolicy: () => ({
    data: {
      enabled: true,
      default_image: "python:3.11-slim",
      network_enabled: true,
      user_id: "65532",
      memory_limit: "512m",
      cpus: "0.5",
      timeout_secs: 30,
    },
  }),
  useMcpServers: () => ({ data: [], isLoading: false }),
  useToolOptions: () => [],
  useDebouncedUpdateAllowedTools: () => hookState.debouncedUpdateAllowedTools,
  useAddMcpServer: () => ({ mutate: vi.fn(), isPending: false }),
  useRemoveMcpServer: () => ({ mutate: vi.fn(), isPending: false }),
  useTestMcpServer: () => ({ mutate: vi.fn(), isPending: false }),
}))

vi.mock("../../../hooks/useAutomationConnectors", () => ({
  useAutomationConnectors: () => ({ data: [] }),
  useConfigureConnector: () => ({ mutate: vi.fn(), isPending: false }),
  useTestConnectorBeforeSave: () => ({ mutate: vi.fn(), isPending: false }),
}))

vi.mock("../../../hooks/useAccountConnectors", () => ({
  useAccountConnectors: () => ({ data: [] }),
}))

vi.mock("../../../hooks/useEscapeKey", () => ({
  useEscapeKey: vi.fn(),
}))

vi.mock("../../../lib/auth", () => ({
  useAuth: () => ({
    user: { id: 7 },
  }),
}))

function renderDrawer(isOpen = true) {
  return render(
    <MemoryRouter>
      <AutomationSettingsDrawer automationId={42} isOpen={isOpen} onClose={vi.fn()} />
    </MemoryRouter>,
  );
}

describe("AutomationSettingsDrawer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hookState.automation = {
      id: 42,
      owner_id: 7,
      name: "Test automation",
      allowed_tools: ["container_exec"],
    };
    hookState.debouncedUpdateAllowedTools = {
      mutate: vi.fn(),
      flush: vi.fn(),
      cancelPending: vi.fn(),
      isPending: false,
      isError: false,
      hasPendingDebounce: false,
      error: null,
      lastSyncedValue: null,
    };
  });

  it("resets unsaved tool draft state when the drawer closes and reopens", async () => {
    const user = userEvent.setup();
    const view = renderDrawer(true);

    const utilityTool = screen.getByLabelText("get_current_time");
    expect(utilityTool).not.toBeChecked();

    await user.click(utilityTool);

    expect(hookState.debouncedUpdateAllowedTools.mutate).toHaveBeenCalledWith(
      ["container_exec", "get_current_time"],
      expect.any(Object),
    );
    expect(utilityTool).toBeChecked();

    view.rerender(
      <MemoryRouter>
        <AutomationSettingsDrawer automationId={42} isOpen={false} onClose={vi.fn()} />
      </MemoryRouter>,
    );

    view.rerender(
      <MemoryRouter>
        <AutomationSettingsDrawer automationId={42} isOpen={true} onClose={vi.fn()} />
      </MemoryRouter>,
    );

    expect(screen.getByLabelText("get_current_time")).not.toBeChecked();
  });

  it("rolls back the local tool draft when saving fails", async () => {
    const user = userEvent.setup();

    hookState.debouncedUpdateAllowedTools.mutate = vi.fn((_: string[] | null, callbacks?: { onError?: (error: Error) => void }) => {
      callbacks?.onError?.(new Error("save failed"));
    });

    renderDrawer(true);

    const utilityTool = screen.getByLabelText("get_current_time");
    await user.click(utilityTool);

    await waitFor(() => expect(utilityTool).not.toBeChecked());
  });
});
