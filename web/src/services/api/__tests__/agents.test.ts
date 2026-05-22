import { beforeEach, describe, expect, it, vi } from "vitest";

const baseMocks = vi.hoisted(() => ({
  request: vi.fn(),
  buildUrl: vi.fn((path: string) => `/api${path}`),
}));

vi.mock("../base", () => baseMocks);

import { fetchAgentSessionProjection, fetchAgentSessionTurns, fetchAgentSessionWorkspace } from "../agents";

describe("fetchAgentSessionTurns", () => {
  beforeEach(() => {
    baseMocks.request.mockReset();
    baseMocks.request.mockResolvedValue({ turns: [], total: 0 });
  });

  it("keeps an explicit offset=0 in the request query string", async () => {
    await fetchAgentSessionTurns("session-1", {
      limit: 10,
      offset: 0,
      order: "desc",
    });

    expect(baseMocks.request).toHaveBeenCalledWith(
      "/timeline/sessions/session-1/turns?limit=10&offset=0&order=desc",
      { method: "GET" },
    );
  });
});

describe("live session fetches", () => {
  beforeEach(() => {
    baseMocks.request.mockReset();
    baseMocks.request.mockResolvedValue({});
  });

  it("bypasses browser cache for workspace refreshes", async () => {
    await fetchAgentSessionWorkspace("session-1", {
      limit: 200,
      branch_mode: "head",
    });

    expect(baseMocks.request).toHaveBeenCalledWith(
      "/timeline/sessions/session-1/workspace?limit=200&branch_mode=head",
      { method: "GET", cache: "no-store" },
    );
  });

  it("bypasses browser cache for projection refreshes", async () => {
    await fetchAgentSessionProjection("session-1", {
      limit: 200,
      offset: 20,
      branch_mode: "head",
    });

    expect(baseMocks.request).toHaveBeenCalledWith(
      "/timeline/sessions/session-1/projection?limit=200&offset=20&branch_mode=head",
      { method: "GET", cache: "no-store" },
    );
  });
});
