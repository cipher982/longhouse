import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAgentSessionProjectionInfinite } from "../useAgentSessions";
import type { AgentSessionProjectionResponse } from "../../services/api";

const apiMocks = vi.hoisted(() => ({
  fetchAgentSessions: vi.fn(),
  fetchAgentSession: vi.fn(),
  fetchAgentSessionThread: vi.fn(),
  fetchAgentSessionProjection: vi.fn(),
  fetchAgentSessionWorkspace: vi.fn(),
  fetchAgentSessionEvents: vi.fn(),
  fetchAgentSessionSummaries: vi.fn(),
  fetchAgentSessionPreview: vi.fn(),
  fetchAgentFilters: vi.fn(),
  fetchRecall: vi.fn(),
}));

vi.mock("../../services/api", () => apiMocks);

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
