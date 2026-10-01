import { fireEvent, render, screen } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import config from "@/shared/lib/config";
import { TestRouter } from "@/shared/test/test-utils";
import LoginPage from "../LoginPage";

const authMocks = vi.hoisted(() => ({
  clearLogoutIntent: vi.fn(),
  hasLogoutIntent: vi.fn(),
  login: vi.fn(),
  loginPassword: vi.fn(),
  refreshAuth: vi.fn(),
  refetchAuthMethods: vi.fn(),
  useAuth: vi.fn(),
  useAuthMethods: vi.fn(),
}));

const refreshMocks = vi.hoisted(() => ({
  clearLogoutBarrier: vi.fn(),
  markLoginAttempt: vi.fn(),
}));

vi.mock("../auth", () => ({
  clearLogoutIntent: authMocks.clearLogoutIntent,
  hasLogoutIntent: authMocks.hasLogoutIntent,
  useAuth: authMocks.useAuth,
  useAuthMethods: authMocks.useAuthMethods,
}));

vi.mock("../auth-refresh", () => refreshMocks);

function renderLogin() {
  return render(
    <TestRouter initialEntries={["/login"]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
      </Routes>
    </TestRouter>,
  );
}

describe("LoginPage", () => {
  beforeEach(() => {
    config.authEnabled = true;
    authMocks.clearLogoutIntent.mockReset();
    authMocks.hasLogoutIntent.mockReset();
    authMocks.hasLogoutIntent.mockReturnValue(true);
    authMocks.login.mockReset();
    authMocks.loginPassword.mockReset();
    authMocks.refreshAuth.mockReset();
    authMocks.refetchAuthMethods.mockReset();
    authMocks.useAuth.mockReturnValue({
      user: null,
      isLoading: false,
      authUnavailable: false,
      login: authMocks.login,
      loginPassword: authMocks.loginPassword,
      refreshAuth: authMocks.refreshAuth,
    });
    authMocks.useAuthMethods.mockReturnValue({
      data: { google: false, password: false, sso: false },
      isLoading: false,
      isError: false,
      refetch: authMocks.refetchAuthMethods,
    });
    refreshMocks.clearLogoutBarrier.mockReset();
    refreshMocks.markLoginAttempt.mockReset();
  });

  it("brands the self-host password form and submits the password", () => {
    authMocks.hasLogoutIntent.mockReturnValue(false);
    authMocks.useAuthMethods.mockReturnValue({
      data: { google: false, password: true, sso: false },
      isLoading: false,
      isError: false,
      refetch: authMocks.refetchAuthMethods,
    });
    authMocks.loginPassword.mockResolvedValue(undefined);
    renderLogin();

    // The product name and mark come first, so the page is recognisably Longhouse.
    expect(screen.getByText("Longhouse")).toBeInTheDocument();
    expect(screen.getByAltText("Longhouse")).toBeInTheDocument();
    const field = screen.getByLabelText("Instance password");
    expect(field).toHaveClass("ui-input");

    fireEvent.change(field, { target: { value: "hunter2" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(authMocks.loginPassword).toHaveBeenCalledWith("hunter2");
  });

  it("clears the logout barrier before starting sign-in again", () => {
    renderLogin();
    fireEvent.click(screen.getByRole("button", { name: "Sign in again" }));

    expect(authMocks.clearLogoutIntent).toHaveBeenCalledOnce();
    expect(refreshMocks.clearLogoutBarrier).toHaveBeenCalledOnce();
    expect(refreshMocks.markLoginAttempt).toHaveBeenCalledOnce();
  });
});
