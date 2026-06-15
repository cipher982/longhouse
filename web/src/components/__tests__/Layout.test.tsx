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

describe("Layout user menu logout", () => {
  it("shows one Log out button on hosted tenants (no separate 'Log out everywhere')", () => {
    authMocks.useAuth.mockReturnValue({
      user: {
        email: "david010@gmail.com",
        display_name: "David",
        role: "ADMIN",
      },
      logout: vi.fn(),
    });
    authMocks.useAuthMethods.mockReturnValue({
      data: {
        sso_url: "https://control.longhouse.ai",
        sso_login_url: "https://control.longhouse.ai/auth/start",
      },
    });

    renderLayout();
    // Open the user dropdown
    const avatar = screen.getByTitle("Account menu");
    avatar.click();

    const logoutButtons = screen.getAllByRole("button", { name: /^Log out$/ });
    expect(logoutButtons).toHaveLength(1);

    // No "Log out everywhere" — the single button does both jobs
    expect(screen.queryByRole("button", { name: "Log out everywhere" })).not.toBeInTheDocument();

    // Switch account is still there for explicit re-login
    expect(screen.getByRole("button", { name: "Switch account" })).toBeInTheDocument();
  });

  it("shows one Log out button on self-host tenants (no CP at all)", () => {
    authMocks.useAuth.mockReturnValue({
      user: {
        email: "selfhost@gmail.com",
        display_name: "Self Host",
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

    renderLayout();
    const avatar = screen.getByTitle("Account menu");
    avatar.click();

    const logoutButtons = screen.getAllByRole("button", { name: /^Log out$/ });
    expect(logoutButtons).toHaveLength(1);

    // No CP-only buttons on self-host
    expect(screen.queryByRole("button", { name: "Log out everywhere" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Switch account" })).not.toBeInTheDocument();
  });
});
