import { beforeEach, describe, expect, it, vi } from "vitest";

const baseMocks = vi.hoisted(() => ({
  request: vi.fn(),
  buildUrl: vi.fn((path: string) => `/api${path}`),
}));

vi.mock("../base", () => baseMocks);

import { fetchAgentSessionTurns } from "../agents";

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
