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
  getToken: vi.fn(() => "mock-token"),
};

vi.mock("../../lib/auth", () => ({
  useAuth: () => mockAuth,
}));

// Mock fetch for API calls
const mockFetch = vi.fn();
global.fetch = mockFetch;

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

    window.localStorage.setItem("zerg_jwt", "mock-token");
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

    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith(
        "/api/users/me",
        expect.objectContaining({
          method: "PUT",
          headers: expect.objectContaining({
            "Authorization": "Bearer mock-token",
            "Content-Type": "application/json",
          }),
          body: JSON.stringify({
            display_name: "Updated Name",
          }),
        })
      );
    });
  });

  it("shows account information", () => {
    renderProfilePage();

    const accountSection = screen.getAllByText("Account Information")[0].closest(".form-section");
    expect(accountSection).not.toBeNull();
    const info = within(accountSection as Element);
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
      expect(mockFetch).toHaveBeenCalledWith(
        "/api/users/me/avatar",
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({
            "Authorization": "Bearer mock-token",
          }),
        })
      );
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
