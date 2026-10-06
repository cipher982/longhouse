import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  agentSessionWorkspaceQueryOptions,
  refreshAgentSessionProjectionTail,
  useAgentSessionProjectionInfinite,
  useAgentSessionWorkspace,
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

function makeWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

function makeProjectionPage({
  total,
  pageOffset,
  startEventId,
  count,
}: {
  total: number;
  pageOffset: number;
  startEventId: number;
  count: number;
}): AgentSessionProjectionResponse {
  const items = Array.from({ length: count }, (_, index) => {
    const eventId = startEventId + index;
    return {
      kind: "event" as const,
      session_id: "session-1",
      timestamp: `2026-04-03T12:${String(index).padStart(2, "0")}:00Z`,
      event: {
        id: eventId,
        role: index % 2 === 0 ? "user" : "assistant",
        content_text: `event ${eventId}`,
        tool_name: null,
        tool_input_json: null,
        tool_output_text: null,
        tool_call_id: null,
        timestamp: `2026-04-03T12:${String(index).padStart(2, "0")}:00Z`,
        in_active_context: true,
      },
    };
  });

  return {
    root_session_id: "session-1",
    focus_session_id: "session-1",
    head_session_id: "session-1",
    path_session_ids: ["session-1"],
    items,
    total,
    page_offset: pageOffset,
    branch_mode: "head",
    abandoned_events: 0,
  };
}

describe("useAgentSessionProjectionInfinite", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("refetches the newest timeline window from the tail anchor after invalidation", async () => {
    const initialPage = makeProjectionPage({
      total: 400,
      pageOffset: 200,
      startEventId: 201,
      count: 200,
    });
    const refetchedTailPage = makeProjectionPage({
      total: 401,
      pageOffset: 201,
      startEventId: 202,
      count: 200,
    });
    apiMocks.fetchAgentSessionProjection.mockResolvedValue(refetchedTailPage);

    const queryClient = new QueryClient({
      defaultOptions: {
        queries: {
          retry: false,
        },
      },
    });

    const { result } = renderHook(
      () => useAgentSessionProjectionInfinite("session-1", { limit: 200, initialPage }),
      { wrapper: makeWrapper(queryClient) },
    );

    expect(result.current.data?.pages[0]?.page_offset).toBe(200);

    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["agent-session-projection-infinite", "session-1"] });
    });

    await waitFor(() => {
      expect(apiMocks.fetchAgentSessionProjection).toHaveBeenCalledWith("session-1", {
        limit: 200,
        anchor: "tail",
        offset: undefined,
        branch_mode: "head",
      });
    });

    await waitFor(() => {
      expect(result.current.data?.pages[0]?.page_offset).toBe(201);
      expect(result.current.data?.pages[0]?.items[0]?.event?.id).toBe(202);
    });
  });

  it("loads older projection slices as exact previous pages without overlap", async () => {
    const initialPage = makeProjectionPage({
      total: 401,
      pageOffset: 201,
      startEventId: 202,
      count: 200,
    });
    const previousPage = makeProjectionPage({
      total: 401,
      pageOffset: 1,
      startEventId: 2,
      count: 200,
    });
    const oldestPage = makeProjectionPage({
      total: 401,
      pageOffset: 0,
      startEventId: 1,
      count: 1,
    });
    apiMocks.fetchAgentSessionProjection
      .mockResolvedValueOnce(previousPage)
      .mockResolvedValueOnce(oldestPage);

    const queryClient = new QueryClient({
      defaultOptions: {
        queries: {
          retry: false,
        },
      },
    });

    const { result } = renderHook(
      () => useAgentSessionProjectionInfinite("session-1", { limit: 200, initialPage }),
      { wrapper: makeWrapper(queryClient) },
    );

    await act(async () => {
      await result.current.fetchPreviousPage();
    });

    await waitFor(() => {
      expect(apiMocks.fetchAgentSessionProjection).toHaveBeenCalledWith("session-1", {
        limit: 200,
        anchor: "start",
        offset: 1,
        branch_mode: "head",
      });
    });

    await act(async () => {
      await result.current.fetchPreviousPage();
    });

    await waitFor(() => {
      expect(apiMocks.fetchAgentSessionProjection).toHaveBeenCalledWith("session-1", {
        limit: 1,
        anchor: "start",
        offset: 0,
        branch_mode: "head",
      });
    });
  });
});

function makeCursorPage(
  eventIds: number[],
  { nextCursor, generationId = "gen-1" }: { nextCursor: string | null; generationId?: string },
): AgentSessionProjectionResponse {
  return {
    ...makeProjectionPage({ total: 7, pageOffset: 0, startEventId: eventIds[0] ?? 0, count: eventIds.length }),
    generation_id: generationId,
    has_more: nextCursor !== null,
    next_cursor: nextCursor,
  };
}

describe("refreshAgentSessionProjectionTail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  async function scrolledUpTranscript() {
    // Server: events 1-6 in pages of two, cursors walk backwards from the tail.
    const server = {
      tail: [5, 6],
      generationId: "gen-1",
    };
    apiMocks.fetchAgentSessionProjection.mockImplementation(
      async (_sessionId: string, options: { cursor?: string }) => {
        if (options.cursor === "cursor-3") return makeCursorPage([3, 4], { nextCursor: "cursor-1" });
        if (options.cursor === "cursor-1") return makeCursorPage([1, 2], { nextCursor: null });
        return makeCursorPage(server.tail, {
          nextCursor: "cursor-3",
          generationId: server.generationId,
        });
      },
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(
      () =>
        useAgentSessionProjectionInfinite("session-1", {
          limit: 2,
          initialPage: makeCursorPage([5, 6], { nextCursor: "cursor-3" }),
        }),
      { wrapper: makeWrapper(queryClient) },
    );
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    await act(async () => {
      await result.current.fetchPreviousPage();
    });
    await act(async () => {
      await result.current.fetchPreviousPage();
    });
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(3));
    apiMocks.fetchAgentSessionProjection.mockClear();
    return { queryClient, result, server };
  }

  const eventIds = (pages: AgentSessionProjectionResponse[] | undefined) =>
    pages?.flatMap((page) => page.items.map((item) => item.event?.id));

  it("refreshes only the tail after the reader scrolled up, keeping older pages", async () => {
    const { queryClient, result, server } = await scrolledUpTranscript();
    server.tail = [6, 7];

    await act(async () => {
      await refreshAgentSessionProjectionTail(queryClient, "session-1");
    });

    // One request, for the tail. A plain invalidation re-ran the oldest page's
    // cursor instead and left only events 1-2 on screen.
    expect(apiMocks.fetchAgentSessionProjection).toHaveBeenCalledTimes(1);
    expect(apiMocks.fetchAgentSessionProjection).toHaveBeenCalledWith("session-1", {
      limit: 2,
      anchor: "tail",
      branch_mode: "head",
    });
    await waitFor(() => expect(eventIds(result.current.data?.pages)).toEqual([1, 2, 3, 4, 5, 6, 7]));
    // Event 6 comes from the fresh page, not the stale one.
    expect(result.current.data?.pages.at(-1)?.items.map((item) => item.event?.id)).toEqual([6, 7]);
  });

  it("starts over from the tail when the projection generation changed", async () => {
    const { queryClient, result, server } = await scrolledUpTranscript();
    server.generationId = "gen-2";

    await act(async () => {
      await refreshAgentSessionProjectionTail(queryClient, "session-1");
    });

    expect(apiMocks.fetchAgentSessionProjection).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(eventIds(result.current.data?.pages)).toEqual([5, 6]));
  });
});

describe("agentSessionWorkspaceQueryOptions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("lands a timeline hover prefetch in the entry the session page reads", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const workspace = { session: { id: "session-1" } };
    apiMocks.fetchAgentSessionWorkspace.mockResolvedValue(workspace);

    // SessionsPage prefetches without share params; the session page passes them as null.
    await queryClient.prefetchQuery(
      agentSessionWorkspaceQueryOptions("session-1", { limit: 200, branch_mode: "head" }),
    );
    const { result } = renderHook(
      () =>
        useAgentSessionWorkspace("session-1", {
          limit: 200,
          branch_mode: "head",
          shared_by: null,
          share_token: null,
        }),
      { wrapper: makeWrapper(queryClient) },
    );

    expect(result.current.data).toBe(workspace);
    expect(apiMocks.fetchAgentSessionWorkspace).toHaveBeenCalledTimes(1);
  });
});
