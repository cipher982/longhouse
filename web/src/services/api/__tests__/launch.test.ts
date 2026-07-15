import { beforeEach, describe, expect, it, vi } from "vitest";

const baseMocks = vi.hoisted(() => ({
  request: vi.fn(),
  buildUrl: vi.fn((path: string) => `/api${path}`),
}));

vi.mock("../base", () => baseMocks);

import { createConsoleSession, fetchWorkspaceSuggestions, listMachines } from "../launch";

describe("fetchWorkspaceSuggestions", () => {
  beforeEach(() => {
    baseMocks.request.mockReset();
    baseMocks.request.mockResolvedValue({ device_id: "cinder", workspaces: [] });
  });

  it("reads workspaces through the cookie-authed timeline surface, not /agents", async () => {
    // The launch modal authenticates with the browser cookie; the /agents
    // sibling is device-token-only and would 401. Pin the path so a drift
    // onto the wrong auth surface fails here instead of on a phone.
    await fetchWorkspaceSuggestions("cinder", { limit: 12 });

    expect(baseMocks.request).toHaveBeenCalledWith(
      "/timeline/machines/cinder/workspaces?limit=12",
    );
    const calledPath = baseMocks.request.mock.calls[0][0] as string;
    expect(calledPath).not.toContain("/agents/");
  });

  it("url-encodes the device id", async () => {
    await fetchWorkspaceSuggestions("dev/box", { limit: 5 });

    expect(baseMocks.request).toHaveBeenCalledWith(
      "/timeline/machines/dev%2Fbox/workspaces?limit=5",
    );
  });

  it("omits the limit query when not provided", async () => {
    await fetchWorkspaceSuggestions("cinder");

    expect(baseMocks.request).toHaveBeenCalledWith("/timeline/machines/cinder/workspaces");
  });
});

describe("listMachines", () => {
  beforeEach(() => {
    baseMocks.request.mockReset();
    baseMocks.request.mockResolvedValue({ machines: [] });
  });

  it("uses the cookie-authed timeline machines directory", async () => {
    await listMachines();
    expect(baseMocks.request).toHaveBeenCalledWith("/timeline/machines");
  });
});

describe("createConsoleSession", () => {
  beforeEach(() => {
    baseMocks.request.mockReset();
    baseMocks.request.mockResolvedValue({
      session_id: "s1",
      thread_id: "t1",
      created: true,
    });
  });

  it("creates an empty Console session without a task", async () => {
    await createConsoleSession({ device_id: "cinder", provider: "claude", cwd: "/Users/me/repo" });

    expect(baseMocks.request).toHaveBeenCalledWith(
      "/sessions/console",
      expect.objectContaining({ method: "POST" }),
    );
    const body = JSON.parse((baseMocks.request.mock.calls[0][1] as RequestInit).body as string);
    expect(body).toMatchObject({ device_id: "cinder", provider: "claude", cwd: "/Users/me/repo" });
    expect(body).not.toHaveProperty("initial_prompt");
    expect(body).not.toHaveProperty("execution_lifetime");
  });
});
