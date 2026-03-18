import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../lib/config", () => ({
  config: { apiBaseUrl: "/api" },
}));

import {
  fetchAutomationAvailableTools,
  fetchAutomationMcpServers,
  fetchAutomationRuns,
} from "../automations";
import {
  fetchAutomationConnectors,
} from "../connectors";

function jsonResponse(payload: unknown) {
  return {
    ok: true,
    status: 200,
    headers: {
      get: () => "application/json",
    },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  } as Response;
}

describe("automation API paths", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("canonical automation helpers hit nested /automations paths", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse([]));
    fetchMock.mockResolvedValueOnce(jsonResponse({ builtin: [], mcp: {} }));
    fetchMock.mockResolvedValueOnce(jsonResponse([]));
    fetchMock.mockResolvedValueOnce(jsonResponse([]));

    await fetchAutomationRuns(7);
    await fetchAutomationAvailableTools(7);
    await fetchAutomationMcpServers(7);
    await fetchAutomationConnectors(7);

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/automations/7/runs?limit=20");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/automations/7/mcp-servers/available-tools");
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/automations/7/mcp-servers/");
    expect(fetchMock.mock.calls[3]?.[0]).toBe("/api/automations/7/connectors");
  });

  it("automation helpers stay the only first-party browser contract", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse([]));
    fetchMock.mockResolvedValueOnce(jsonResponse([]));

    await fetchAutomationRuns(9);
    await fetchAutomationConnectors(9);

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/automations/9/runs?limit=20");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/automations/9/connectors");
  });
});
