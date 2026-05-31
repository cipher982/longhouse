import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Layout from "../Layout";
import { TestRouter } from "../../test/test-utils";

const authMocks = vi.hoisted(() => ({
  useAuth: vi.fn(),
  useAuthMethods: vi.fn(),
}));

vi.mock("../../lib/auth", () => ({
  useAuth: authMocks.useAuth,
  useAuthMethods: authMocks.useAuthMethods,
}));

vi.mock("../../components/confirm", () => ({
  useConfirm: () => vi.fn().mockResolvedValue(true),
}));

vi.mock("../../lib/apiHealth", () => ({
  useApiHealth: () => null,
}));

vi.mock("../../lib/useWebSocket", () => ({
  ConnectionStatus: {
    CONNECTED: "connected",
    ERROR: "error",
  },
  ConnectionStatusIndicator: () => <span data-testid="connection-status-indicator" />,
}));

vi.mock("../../hooks/useDocumentVisible", () => ({
  useDocumentVisible: () => false,
}));

function renderLayout() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={["/timeline"]}>
        <Layout>
          <main>Timeline content</main>
        </Layout>
      </TestRouter>
    </QueryClientProvider>,
  );
}

describe("Layout mobile navigation", () => {
  it("treats the mobile nav as a modal sheet with a dismissible scrim", async () => {
    authMocks.useAuth.mockReturnValue({
      user: {
        email: "demo@gmail.com",
        display_name: "Demo User",
        role: "ADMIN",
      },
      logout: vi.fn(),
    });
    authMocks.useAuthMethods.mockReturnValue({
      data: {
        sso_url: null,
        sso_login_url: null,
      },
    });

    const user = userEvent.setup();
    const { container } = renderLayout();

    const drawer = screen.getByLabelText("Mobile navigation");
    const toggle = screen.getByRole("button", { name: "Open menu" });
    const scrim = container.querySelector(".mobile-nav-scrim");

    expect(drawer).toHaveAttribute("aria-hidden", "true");
    expect(drawer).not.toHaveClass("open");
    expect(scrim).not.toHaveClass("visible");

    await user.click(toggle);

    expect(drawer).toHaveAttribute("aria-hidden", "false");
    expect(drawer).toHaveClass("open");
    expect(scrim).toHaveClass("visible");

    if (!scrim) {
      throw new Error("Expected mobile nav scrim to render");
    }

    await user.click(scrim);

    expect(drawer).toHaveAttribute("aria-hidden", "true");
    expect(drawer).not.toHaveClass("open");
    expect(scrim).not.toHaveClass("visible");
  });
});
