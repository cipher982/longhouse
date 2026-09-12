import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import ProfilePage from "../ProfilePage";

// Mock the auth hook
const mockUser = {
  id: 1,
  email: "test@example.com",
  display_name: "Test User",
  avatar_url: "https://example.com/avatar.jpg",
  is_active: true,
  created_at: "2024-01-01T00:00:00Z",
  last_login: "2024-01-02T00:00:00Z",
};

const mockAuth = {
  user: mockUser,
  isAuthenticated: true,
  isLoading: false,
  login: vi.fn(),
  logout: vi.fn(),
};

vi.mock("../../lib/auth", () => ({
  useAuth: () => mockAuth,
}));

// Mock fetch for API calls
const mockFetch = vi.fn();
global.fetch = mockFetch;

function getRequestForPath(path: string): Request {
  const call = mockFetch.mock.calls.find(([input]) => {
    const url = input instanceof Request ? input.url : String(input);
    return new URL(url, window.location.origin).pathname === path;
  });
  expect(call).toBeDefined();
  const [input, init] = call ?? [];
  if (input instanceof Request) return input;
  if (input === undefined) throw new Error(`No fetch request for ${path}`);
  return new Request(input as RequestInfo, init as RequestInit | undefined);
}

function renderProfilePage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <ProfilePage />
    </QueryClientProvider>
  );
}

describe("ProfilePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    mockFetch.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();

      if (url === "/api/users/me") {
        return {
          ok: true,
          json: () => Promise.resolve({ ...mockUser, display_name: "Updated Name" }),
        } as Response;
      }

      return {
        ok: true,
        json: () => Promise.resolve(mockUser),
      } as Response;
    });
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("renders user profile form with current data", async () => {
    renderProfilePage();

    expect(screen.getByText("User Profile")).toBeInTheDocument();
    expect(screen.getByLabelText("Display Name")).toHaveValue("Test User");
    expect(screen.getByLabelText("Email Address")).toHaveValue("test@example.com");
    expect(screen.getByLabelText("Avatar URL")).toHaveValue("https://example.com/avatar.jpg");
  });

  it("allows updating display name", async () => {
    renderProfilePage();
    const user = userEvent.setup();

    const displayNameInput = screen.getByLabelText("Display Name");
    await user.clear(displayNameInput);
    await user.type(displayNameInput, "Updated Name");

    const saveButtons = screen.getAllByRole("button", { name: "Save Changes" });
    const saveButton = saveButtons[0]; // Take first button due to StrictMode double rendering
    await user.click(saveButton);

    await waitFor(async () => {
      const request = getRequestForPath("/api/users/me");
      expect(request.method).toBe("PUT");
      expect(request.credentials).toBe("include");
      expect(request.headers.get("Content-Type")).toBe("application/json");
      await expect(request.clone().json()).resolves.toEqual({
        display_name: "Updated Name",
      });
    });
  });

  it("shows account information", () => {
    renderProfilePage();

    // Account Information is now rendered inside a Card (ui primitives)
    const accountCard = screen.getAllByText("Account Information")[0].closest(".ui-card");
    expect(accountCard).not.toBeNull();
    const info = within(accountCard as Element);
    expect(info.getByText("User ID:")).toBeInTheDocument();
    expect(info.getByText(String(mockUser.id))).toBeInTheDocument();
    const expectedMemberSince = new Date(mockUser.created_at).toLocaleDateString();
    expect(info.getByText(expectedMemberSince)).toBeInTheDocument();
  });

  it("handles avatar file upload", async () => {
    renderProfilePage();
    const user = userEvent.setup();

    // Mock successful file upload
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ ...mockUser, avatar_url: "new-avatar.jpg" }),
    } as Response);

    const file = new File(["avatar"], "avatar.png", { type: "image/png" });
    const fileInput = screen.getByLabelText("Choose Avatar");

    await user.upload(fileInput, file);

    await waitFor(() => {
      const request = getRequestForPath("/api/users/me/avatar");
      expect(request.method).toBe("POST");
      expect(request.credentials).toBe("include");
    });
  });

  it("resets form to original values", async () => {
    renderProfilePage();
    const user = userEvent.setup();

    const displayNameInput = screen.getByLabelText("Display Name");
    await user.clear(displayNameInput);
    await user.type(displayNameInput, "Changed Name");

    const resetButton = screen.getAllByRole("button", { name: /Reset Changes/i })[0];
    await user.click(resetButton);

    expect(displayNameInput).toHaveValue("Test User");
  });
});
