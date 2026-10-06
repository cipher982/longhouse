import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  applyProjectionDelta,
  projectionDeltaAnchor,
  refreshAgentSessionProjectionTail,
  useAgentSessionProjectionInfinite,
} from "../useAgentSessions";
import type { AgentSessionProjectionResponse } from "../index";

const apiMocks = vi.hoisted(() => ({
  fetchAgentSessions: vi.fn(),
  fetchAgentSessionProjection: vi.fn(),
  fetchAgentSessionWorkspace: vi.fn(),
  fetchAgentSessionSummaries: vi.fn(),
  fetchAgentSessionPreview: vi.fn(),
  fetchAgentFilters: vi.fn(),
  fetchRecall: vi.fn(),
  fetchRecallContext: vi.fn(),
}));

vi.mock("../index", () => apiMocks);

function page(
  ids: number[],
  {
    generationId = "gen-1",
    hasMore = false,
    nextCursor = null,
    running = [] as number[],
  }: { generationId?: string; hasMore?: boolean; nextCursor?: string | null; running?: number[] } = {},
): AgentSessionProjectionResponse {
  return {
    root_session_id: "s",
    focus_session_id: "s",
    head_session_id: "s",
    path_session_ids: ["s"],
    items: ids.map((id) => ({
      kind: "event" as const,
      session_id: "s",
      timestamp: `2026-10-06T12:00:${String(id % 60).padStart(2, "0")}Z`,
      event: {
        id,
        cursor: `c-${id}`,
        role: "assistant",
        content_text: `event ${id}`,
        tool_input_json: null,
        tool_output_text: null,
        tool_call_id: null,
        tool_call_state: running.includes(id) ? ("running" as const) : null,
        timestamp: `2026-10-06T12:00:${String(id % 60).padStart(2, "0")}Z`,
      },
    })),
    total: ids.length,
    generation_id: generationId,
    has_more: hasMore,
    next_cursor: nextCursor,
  };
}

const range = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => from + i);

describe("projectionDeltaAnchor", () => {
  it("re-reads the last twenty rows", () => {
    expect(projectionDeltaAnchor(page(range(1, 25)))).toEqual({ index: 5, cursor: "c-5" });
  });

  it("starts before an older tool call that is still running", () => {
    expect(projectionDeltaAnchor(page(range(1, 25), { running: [3] }))).toEqual({ index: 2, cursor: "c-2" });
  });

  it("has no anchor when the overlap covers the whole tail", () => {
    expect(projectionDeltaAnchor(page(range(1, 12)))).toBeNull();
  });
});

describe("applyProjectionDelta", () => {
  const held = {
    pages: [page(range(1, 3), { hasMore: true, nextCursor: "older" }), page(range(4, 25))],
    pageParams: [{ anchor: "tail" }, { anchor: "tail" }],
  };

  it("splices new rows after the anchor and keeps older pages and the tail's own cursor", () => {
    const next = applyProjectionDelta(held, page(range(10, 27)), "c-9");
    const ids = next?.pages.flatMap((p) => p.items.map((item) => item.event?.id));
    expect(ids).toEqual(range(1, 27));
    expect(next?.pages[0]).toBe(held.pages[0]);
    expect(next?.pages[1].next_cursor).toBeNull();
  });

  it("finds the anchor where it is now, after another refresh moved the tail", () => {
    const moved = { pages: [page(range(4, 30))], pageParams: [{ anchor: "tail" }] };
    const next = applyProjectionDelta(moved, page(range(10, 31)), "c-9");
    expect(next?.pages[0].items.map((item) => item.event?.id)).toEqual(range(4, 31));
  });

  it("keeps newer rows a later refresh already landed instead of an older delta", () => {
    const newer = { pages: [page(range(4, 30))], pageParams: [{ anchor: "tail" }] };
    expect(applyProjectionDelta(newer, page(range(10, 26)), "c-9")).toBe(newer);
  });

  it("refuses a delta from another generation, one that didn't reach the newest event, or a lost anchor", () => {
    expect(applyProjectionDelta(held, page(range(10, 27), { generationId: "gen-2" }), "c-9")).toBeNull();
    expect(applyProjectionDelta(held, page(range(10, 27), { hasMore: true }), "c-9")).toBeNull();
    expect(applyProjectionDelta(held, page(range(10, 27)), "c-999")).toBeNull();
  });
});

describe("refreshAgentSessionProjectionTail with a delta", () => {
  beforeEach(() => vi.clearAllMocks());

  async function liveTranscript(sessionId: string, server: { tail: AgentSessionProjectionResponse }) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(
      () => useAgentSessionProjectionInfinite(sessionId, { limit: 200, initialPage: server.tail }),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
        ),
      },
    );
    // Read `data` here: the hook re-renders only for properties it has handed out.
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(1));
    // The first refresh reads the whole tail and starts the correction clock.
    await act(async () => {
      await refreshAgentSessionProjectionTail(queryClient, sessionId);
    });
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    apiMocks.fetchAgentSessionProjection.mockClear();
    return { queryClient, result };
  }

  it("asks only for events after the newest settled row and splices them in", async () => {
    const server = { tail: page(range(1, 25)) };
    apiMocks.fetchAgentSessionProjection.mockImplementation(async (_id: string, options: { anchor?: string; cursor?: string }) =>
      options.anchor === "start" && options.cursor === "c-5" ? page(range(6, 27)) : server.tail,
    );
    const { queryClient, result } = await liveTranscript("delta-session", server);

    await act(async () => {
      await refreshAgentSessionProjectionTail(queryClient, "delta-session");
    });

    expect(apiMocks.fetchAgentSessionProjection).toHaveBeenCalledTimes(1);
    expect(apiMocks.fetchAgentSessionProjection).toHaveBeenCalledWith("delta-session", {
      limit: 200,
      anchor: "start",
      cursor: "c-5",
      branch_mode: "head",
    });
    const ids = () => result.current.data?.pages.flatMap((p) => p.items.map((item) => item.event?.id));
    await waitFor(() => expect(ids()).toEqual(range(1, 27)));
  });

  it("reads the whole tail when the delta is refused (a stale generation)", async () => {
    const server = { tail: page(range(1, 25)) };
    apiMocks.fetchAgentSessionProjection.mockImplementation(async (_id: string, options: { anchor?: string }) => {
      if (options.anchor === "start") throw Object.assign(new Error("stale_generation"), { status: 409 });
      return server.tail;
    });
    const { queryClient } = await liveTranscript("stale-session", server);
    server.tail = page(range(1, 26), { generationId: "gen-2" });

    await act(async () => {
      await refreshAgentSessionProjectionTail(queryClient, "stale-session");
    });

    const calls = apiMocks.fetchAgentSessionProjection.mock.calls.map((call) => call[1]);
    expect(calls[0]).toMatchObject({ anchor: "start", cursor: "c-5" });
    expect(calls.length).toBeGreaterThanOrEqual(2);
    expect(calls.at(-1)).toMatchObject({ limit: 200, anchor: "tail" });
    expect(calls.at(-1)?.cursor).toBeUndefined();
  });
});
