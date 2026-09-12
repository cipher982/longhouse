import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.unmock("../auth");

import { AuthProvider, clearLogoutIntent, hasLogoutIntent, useAuth } from "../auth";
import { clearLoginAttempt } from "../auth-refresh";

const LOGGED_OUT_SESSION_KEY = "longhouse:logged-out";

function AuthHarness() {
  const { user, logout } = useAuth();
  return (
    <>
      <span>{user?.email ?? "signed out"}</span>
      <button type="button" onClick={() => void logout()}>
        Log out
      </button>
    </>
  );
}

describe("logout intent", () => {
  beforeEach(() => {
    clearLoginAttempt();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    clearLogoutIntent();
  });

  it("does not treat a legacy timestamp as the current logout sentinel", () => {
    window.localStorage.setItem(LOGGED_OUT_SESSION_KEY, String(Date.now()));

    expect(hasLogoutIntent()).toBe(false);
  });

  it("recognizes the current sentinel in either storage scope", () => {
    window.localStorage.setItem(LOGGED_OUT_SESSION_KEY, "1");
    expect(hasLogoutIntent()).toBe(true);

    window.localStorage.removeItem(LOGGED_OUT_SESSION_KEY);
    window.sessionStorage.setItem(LOGGED_OUT_SESSION_KEY, "1");
    expect(hasLogoutIntent()).toBe(true);
  });

  it("does not consume a ready marker without a login attempt", async () => {
    window.localStorage.setItem(LOGGED_OUT_SESSION_KEY, "1");
    document.cookie = "lh_login_ready=1; Path=/";

    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <AuthHarness />
        </AuthProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("signed out")).toBeInTheDocument();
    expect(document.cookie).toContain("lh_login_ready=1");
    expect(hasLogoutIntent()).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
    document.cookie = "lh_login_ready=; Max-Age=0; Path=/";
  });

  it("does not accept a handoff marker from before the latest logout", async () => {
    window.localStorage.setItem(LOGGED_OUT_SESSION_KEY, "1");
    window.localStorage.setItem("longhouse:logout-generation", "2");
    document.cookie = "lh_login_ready=1; Path=/";
    window.sessionStorage.setItem("longhouse:login-attempt", "1");

    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <AuthHarness />
        </AuthProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("signed out")).toBeInTheDocument();
    expect(hasLogoutIntent()).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
    document.cookie = "lh_login_ready=; Max-Age=0; Path=/";
  });

  it("accepts a dashboard handoff using the server-side generation marker", async () => {
    window.localStorage.setItem(LOGGED_OUT_SESSION_KEY, "1");
    window.localStorage.setItem("longhouse:logout-generation", "2");
    window.localStorage.setItem("longhouse:logout-barrier", String(Date.now() + 30_000));
    document.cookie = "lh_login_attempt=2; Path=/";
    document.cookie = "lh_login_ready=2; Path=/";
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          authenticated: true,
          user: {
            id: 1,
            email: "dashboard@example.com",
            is_active: true,
            created_at: new Date(0).toISOString(),
          },
        }),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <AuthHarness />
        </AuthProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("dashboard@example.com")).toBeInTheDocument();
    expect(window.localStorage.getItem(LOGGED_OUT_SESSION_KEY)).toBeNull();
    document.cookie = "lh_login_ready=; Max-Age=0; Path=/";
    document.cookie = "lh_login_attempt=; Max-Age=0; Path=/";
  });

  it("accepts a completed hosted handoff despite a stale logout intent", async () => {
    window.localStorage.setItem(LOGGED_OUT_SESSION_KEY, "1");
    document.cookie = "lh_login_ready=1; Path=/";
    window.sessionStorage.setItem("longhouse:login-attempt", "1");
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          authenticated: true,
          user: {
            id: 1,
            email: "handoff@example.com",
            is_active: true,
            created_at: new Date(0).toISOString(),
          },
        }),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <AuthHarness />
        </AuthProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("handoff@example.com")).toBeInTheDocument();
    expect(window.localStorage.getItem(LOGGED_OUT_SESSION_KEY)).toBeNull();
    expect(document.cookie).not.toContain("lh_login_ready");
  });

  it("clears the local session even when the server cannot confirm logout", async () => {
    const fetchMock = vi.fn<typeof fetch>();
    fetchMock
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            authenticated: true,
            user: {
              id: 1,
              email: "user@example.com",
              is_active: true,
              created_at: new Date(0).toISOString(),
            },
          }),
          { status: 200 },
        ),
      )
      .mockRejectedValueOnce(new TypeError("network unavailable"));
    vi.stubGlobal("fetch", fetchMock);

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <AuthHarness />
        </AuthProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("user@example.com")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Log out" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(screen.getByText("signed out")).toBeInTheDocument();
    expect(window.localStorage.getItem(LOGGED_OUT_SESSION_KEY)).toBe("1");
  });
});
