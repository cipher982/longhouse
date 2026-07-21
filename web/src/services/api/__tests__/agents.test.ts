import { beforeEach, describe, expect, it, vi } from "vitest";

const baseMocks = vi.hoisted(() => ({
  request: vi.fn(),
  buildUrl: vi.fn((path: string) => `/api${path}`),
}));

vi.mock("../base", () => baseMocks);

import {
  createSessionShare,
  fetchAgentSessions,
  fetchAgentSessionProjection,
  fetchAgentSessionTurns,
  fetchAgentSessionWorkspace,
  fetchSessionSharePreview,
  resolveSessionShare,
  revokeSessionShare,
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
    });

    const result = await fetchAgentSessions({ query: "durable storage", limit: 50 });

    expect(result.sessions).toEqual([card]);
    expect(result.sessions[0].head).toBe(head);
    expect(result.query_grouping_mode).toBe("grouped_results");
  });

  it("still groups legacy raw query hits by their logical thread", async () => {
    const rawSession = {
      id: "session-1",
      thread_root_session_id: "thread-1",
      thread_head_session_id: "session-1",
      thread_continuation_count: 1,
      timeline_anchor_at: "2026-07-20T12:00:00Z",
      last_activity_at: "2026-07-20T12:00:00Z",
      started_at: "2026-07-20T11:00:00Z",
      origin_label: "laptop",
      environment: "development",
      is_writable_head: true,
    };
    baseMocks.request.mockResolvedValue({
      sessions: [rawSession],
      total: 1,
      has_real_sessions: true,
    });

    const result = await fetchAgentSessions({ query: "legacy", limit: 50 });

    expect(result.sessions).toHaveLength(1);
    expect(result.sessions[0]).toMatchObject({
      thread_id: "thread-1",
      head: rawSession,
      detail: rawSession,
      root: rawSession,
    });
  });
});

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

  it("passes share attribution params to workspace refreshes", async () => {
    await fetchAgentSessionWorkspace("session-1", {
      limit: 200,
      branch_mode: "head",
      shared_by: 7,
      share_token: "lhshr_abc.def",
    });

    expect(baseMocks.request).toHaveBeenCalledWith(
      "/timeline/sessions/session-1/workspace?limit=200&branch_mode=head&shared_by=7&share_token=lhshr_abc.def",
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

describe("session share links", () => {
  beforeEach(() => {
    baseMocks.request.mockReset();
    baseMocks.request.mockResolvedValue({});
  });

  it("creates a signed share through the timeline session surface", async () => {
    await createSessionShare("session-1", { note: "review", expires_in_days: 14 });

    expect(baseMocks.request).toHaveBeenCalledWith(
      "/timeline/sessions/session-1/shares",
      {
        method: "POST",
        body: JSON.stringify({ note: "review", expires_in_days: 14 }),
      },
    );
  });

  it("resolves, previews, and revokes share links through their dedicated routes", async () => {
    await resolveSessionShare("lhshr_a.b");
    await fetchSessionSharePreview("lhshr_a.b");
    await revokeSessionShare(12);

    expect(baseMocks.request).toHaveBeenNthCalledWith(
      1,
      "/timeline/session-shares/lhshr_a.b/resolve",
      { method: "GET", cache: "no-store" },
    );
    expect(baseMocks.request).toHaveBeenNthCalledWith(
      2,
      "/public/session-shares/lhshr_a.b/preview",
      { method: "GET", cache: "no-store" },
    );
    expect(baseMocks.request).toHaveBeenNthCalledWith(
      3,
      "/timeline/session-shares/12",
      { method: "DELETE" },
    );
  });
});
