import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPage from "../SettingsPage";

const apiMocks = vi.hoisted(() => ({
  getUserContext: vi.fn(),
  updateUserContext: vi.fn(),
}));

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return {
    ...actual,
    ...apiMocks,
  };
});

vi.mock("../../components/EmailConfigCard", () => ({
  default: () => <div>Email Config</div>,
}));

vi.mock("../../components/LlmProviderCard", () => ({
  default: () => <div>LLM Providers</div>,
}));

vi.mock("react-hot-toast", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <SettingsPage />
    </QueryClientProvider>,
  );
}

describe("SettingsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.getUserContext.mockResolvedValue({
      context: {
        display_name: "David",
        role: "Founder",
        location: "Montevideo",
        description: "Builds Longhouse",
        custom_instructions: "Keep it concise",
        servers: [],
        integrations: {},
        tools: {
          location: true,
          whoop: true,
          obsidian: true,
          oikos: true,
          custom_tool: false,
        },
      },
    });
    apiMocks.updateUserContext.mockResolvedValue({
      context: {
        display_name: "David",
      },
    });
  });

  it("resets draft edits and preserves unknown tool keys on save", async () => {
    const user = userEvent.setup();

    renderPage();

    const displayNameInput = await screen.findByDisplayValue("David");
    await user.clear(displayNameInput);
    await user.type(displayNameInput, "Changed");
    expect(displayNameInput).toHaveValue("Changed");

    await user.click(screen.getByRole("button", { name: "Reset Changes" }));
    expect(screen.getByDisplayValue("David")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save Settings" }));

    await waitFor(() =>
      expect(apiMocks.updateUserContext).toHaveBeenCalledWith(
        expect.objectContaining({
          display_name: "David",
          role: "Founder",
          location: "Montevideo",
          description: "Builds Longhouse",
          custom_instructions: "Keep it concise",
          tools: {
            location: true,
            whoop: true,
            obsidian: true,
            oikos: true,
            custom_tool: false,
          },
        }),
        expect.anything(),
      ),
    );
  });
});
