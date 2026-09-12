import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.unmock("../auth");

import { AuthProvider, clearLogoutIntent, hasLogoutIntent, useAuth } from "../auth";

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

  it("accepts a completed hosted handoff despite a stale logout intent", async () => {
    window.localStorage.setItem(LOGGED_OUT_SESSION_KEY, "1");
    document.cookie = "lh_login_ready=1; Path=/";
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
