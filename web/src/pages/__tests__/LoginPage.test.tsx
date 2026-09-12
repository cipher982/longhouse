import { fireEvent, render, screen } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import config from "../../lib/config";
import { TestRouter } from "../../test/test-utils";
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

vi.mock("../../lib/auth", () => ({
  clearLogoutIntent: authMocks.clearLogoutIntent,
  hasLogoutIntent: authMocks.hasLogoutIntent,
  useAuth: authMocks.useAuth,
  useAuthMethods: authMocks.useAuthMethods,
}));

vi.mock("../../lib/auth-refresh", () => refreshMocks);

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

  it("clears the logout barrier before starting sign-in again", () => {
    renderLogin();
    fireEvent.click(screen.getByRole("button", { name: "Sign in again" }));

    expect(authMocks.clearLogoutIntent).toHaveBeenCalledOnce();
    expect(refreshMocks.clearLogoutBarrier).toHaveBeenCalledOnce();
    expect(refreshMocks.markLoginAttempt).toHaveBeenCalledOnce();
  });
});
