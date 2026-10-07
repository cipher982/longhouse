import { beforeEach, describe, expect, it, vi } from "vitest";

const baseMocks = vi.hoisted(() => ({
  request: vi.fn(),
  buildUrl: vi.fn((path: string) => `/api${path}`),
}));

vi.mock("../base", () => baseMocks);

import {
  createSessionResumeIntent,
  fetchAgentSessions,
  fetchAgentSessionProjection,
  fetchAgentSessionWorkspace,
  respondToPauseRequest,
} from "../agents";

describe("query timeline normalization", () => {
  beforeEach(() => {
    baseMocks.request.mockReset();
  });

  it("preserves canonical storage-v2 thread cards instead of grouping them again", async () => {
    const head = {
      id: "head-1",
      session_state: { pending_interaction: null },
    };
    const card = {
      thread_id: "thread-1",
      timeline_anchor_at: "2026-07-20T12:00:00Z",
      head,
      detail: head,
      root: head,
      continuation_count: 1,
      started_origin_label: "laptop",
      head_origin_label: "laptop",
    };
    baseMocks.request.mockResolvedValue({
      sessions: [card],
      total: 1,
      has_real_sessions: true,
      coverage: { indexed_sessions: 8, expected_sessions: 10, complete: false, lagging_sessions: 2 },
    });

    const result = await fetchAgentSessions({ query: "durable storage", limit: 50 });

    expect(result.sessions).toEqual([card]);
    expect(result.sessions[0].head).toBe(head);
    expect(result.query_grouping_mode).toBe("grouped_results");
    expect(result.coverage).toEqual({ indexed_sessions: 8, expected_sessions: 10, complete: false, lagging_sessions: 2 });
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
      "/timeline/sessions/session-1/workspace?detail=lite&limit=200&branch_mode=head",
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
      "/timeline/sessions/session-1/projection?detail=lite&limit=200&offset=20&branch_mode=head",
      { method: "GET", cache: "no-store" },
    );
  });

  it("requests a fresh Resume terminal handoff", async () => {
    await createSessionResumeIntent("session-1");

    expect(baseMocks.request).toHaveBeenCalledWith(
      "/timeline/sessions/session-1/resume-intent",
      { method: "POST" },
    );
  });
});

describe("pause request answers", () => {
  beforeEach(() => {
    baseMocks.request.mockReset();
    baseMocks.request.mockResolvedValue({});
  });

  it("posts to the session-chat route the server mounts", async () => {
    await respondToPauseRequest("session-1", "pause-1", { response: "yes" } as never);

    expect(baseMocks.request).toHaveBeenCalledWith(
      "/sessions/session-1/pause-requests/pause-1/response",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
