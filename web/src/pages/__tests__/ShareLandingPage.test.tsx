import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TestRouter } from "../../test/test-utils";
import ShareLandingPage from "../ShareLandingPage";

const authMocks = vi.hoisted(() => ({
  useAuth: vi.fn(),
}));

const apiMocks = vi.hoisted(() => ({
  fetchSessionSharePreview: vi.fn(),
  resolveSessionShare: vi.fn(),
}));

vi.mock("../../lib/auth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/auth")>();
  return {
    ...actual,
    useAuth: authMocks.useAuth,
  };
});

vi.mock("../../lib/readiness-contract", () => ({
  useReadinessFlag: vi.fn(),
}));

vi.mock("../../services/api/agents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api/agents")>();
  return {
    ...actual,
    fetchSessionSharePreview: apiMocks.fetchSessionSharePreview,
    resolveSessionShare: apiMocks.resolveSessionShare,
  };
});

function TimelineProbe() {
  const location = useLocation();
  return <div data-testid="timeline-probe">{location.pathname + location.search}</div>;
}

function renderShareLanding(initialEntry = "/share/lhshr_test") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/share/:token" element={<ShareLandingPage />} />
          <Route path="/timeline/:sessionId" element={<TimelineProbe />} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>,
  );
}

describe("ShareLandingPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authMocks.useAuth.mockReturnValue({
      user: null,
      isAuthenticated: false,
      isLoading: false,
      login: vi.fn(),
      logout: vi.fn(),
      refreshAuth: vi.fn(),
    });
    apiMocks.fetchSessionSharePreview.mockResolvedValue({
      provider: "codex",
      device_name: "cinder",
      started_at: "2026-04-20T12:00:00Z",
      ended_at: null,
      expires_at: "2026-05-20T12:00:00Z",
      note: "Review the share flow.",
      sharer: { id: 2, display_name: "David Rose" },
    });
    apiMocks.resolveSessionShare.mockResolvedValue({
      session_id: "session-123",
      share_id: 42,
      expires_at: "2026-05-20T12:00:00Z",
      note: "Review the share flow.",
      sharer: { id: 2, display_name: "David Rose" },
    });
  });

  it("shows anonymous users a safe preview without resolving the session", async () => {
    renderShareLanding();

    expect(await screen.findByText("David Rose shared a Codex session")).toBeInTheDocument();
    expect(screen.getByText("Review the share flow.")).toBeInTheDocument();
    expect(screen.getByTestId("share-landing-login-button")).toBeInTheDocument();
    expect(apiMocks.fetchSessionSharePreview).toHaveBeenCalledWith("lhshr_test");
    expect(apiMocks.resolveSessionShare).not.toHaveBeenCalled();
  });

  it("resolves authenticated users and redirects to the timeline with the share token", async () => {
    authMocks.useAuth.mockReturnValue({
      user: { id: 1, email: "viewer@example.com", display_name: "Viewer" },
      isAuthenticated: true,
      isLoading: false,
      login: vi.fn(),
      logout: vi.fn(),
      refreshAuth: vi.fn(),
    });

    renderShareLanding("/share/lhshr_auth");

    await waitFor(() => {
      expect(screen.getByTestId("timeline-probe")).toHaveTextContent(
        "/timeline/session-123?share_token=lhshr_auth",
      );
    });
    expect(apiMocks.resolveSessionShare).toHaveBeenCalledWith("lhshr_auth");
  });

  it("shows an unavailable state for dead share links", async () => {
    apiMocks.fetchSessionSharePreview.mockRejectedValueOnce(new Error("Share link expired"));

    renderShareLanding();

    expect(await screen.findByTestId("share-landing-error")).toHaveTextContent(
      "This share link could not be opened.",
    );
  });
});
