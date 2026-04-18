import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as loginRedirect from "../loginRedirect";
import { fetchWithRefresh } from "../auth-refresh";

describe("fetchWithRefresh", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    window.history.replaceState({}, "", "/timeline/abc?view=compact#notes");
  });

  afterEach(() => {
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
});
