import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as loginRedirect from "../loginRedirect";
import { beginLogoutBarrier, clearLogoutBarrier, fetchWithRefresh } from "../auth-refresh";

describe("fetchWithRefresh", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    clearLogoutBarrier();
    vi.stubGlobal("fetch", vi.fn());
    window.history.replaceState({}, "", "/timeline/abc?view=compact#notes");
  });

  afterEach(() => {
    clearLogoutBarrier();
    window.localStorage.clear();
    window.sessionStorage.clear();
    delete window.LonghouseNativeAuth;
    vi.restoreAllMocks();
  });

  it("hands auth back to the native bridge when refresh fails", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(null, { status: 401 }));

    const requestAuth = vi.fn();
    window.LonghouseNativeAuth = { requestAuth };
    const replaceSpy = vi.spyOn(loginRedirect, "replaceWithLoginUrl").mockImplementation(() => {});

    const response = await fetchWithRefresh("/api/users/me");

    expect(response.status).toBe(401);
    expect(requestAuth).toHaveBeenCalledWith({
      return_to: "/timeline/abc?view=compact#notes",
    });
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({
        headers: { "X-Longhouse-Auth": "1" },
      }),
    );
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("falls back to the browser login route when no native bridge exists", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(null, { status: 401 }));

    const replaceSpy = vi.spyOn(loginRedirect, "replaceWithLoginUrl").mockImplementation(() => {});

    const response = await fetchWithRefresh("/api/users/me");

    expect(response.status).toBe(401);
    expect(replaceSpy).toHaveBeenCalledWith("/timeline/abc?view=compact#notes");
  });

  it("replays a one-shot request body after a successful refresh", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(null, { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    const response = await fetchWithRefresh("/api/users/me", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "text/plain" },
      body: "one-shot-body",
    });
    const initialRequest = fetchMock.mock.calls[0]?.[0];
    expect(initialRequest).toBeInstanceOf(Request);
    expect((initialRequest as Request).headers.get("X-Longhouse-Auth")).toBe("1");

    expect(response.status).toBe(204);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const replay = fetchMock.mock.calls[2]?.[0];
    expect(replay).toBeInstanceOf(Request);
    expect((replay as Request).credentials).toBe("include");
    expect(await (replay as Request).text()).toBe("one-shot-body");
  });

  it("does not turn a control-plane outage into a logout", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(null, { status: 503 }));

    const replaceSpy = vi.spyOn(loginRedirect, "replaceWithLoginUrl").mockImplementation(() => {});
    const requestAuth = vi.fn();
    window.LonghouseNativeAuth = { requestAuth };

    const response = await fetchWithRefresh("/api/users/me");

    expect(response.status).toBe(401);
    expect(replaceSpy).not.toHaveBeenCalled();
    expect(requestAuth).not.toHaveBeenCalled();
  });
  it("does not start a refresh after logout fencing begins", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 401 }));

    beginLogoutBarrier();
    const response = await fetchWithRefresh("/api/users/me");

    expect(response.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

});
