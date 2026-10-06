/**
 * React Query hooks for Longhouse agent session data.
 *
 * Used by the Session Picker modal and other session UIs.
 */

import {
  useInfiniteQuery,
  useQuery,
  keepPreviousData,
  type InfiniteData,
  type QueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";
import {
  fetchAgentSessions,
  fetchAgentSessionProjection,
  fetchAgentSessionWorkspace,
  fetchAgentSessionSummaries,
  fetchAgentSessionPreview,
  fetchAgentFilters,
  fetchRecall,
  fetchRecallContext,
  type AgentSessionFilters,
  type TimelineSessionsListResponse,
  type AgentSessionProjectionResponse,
  type AgentSessionWorkspaceResponse,
  type AgentSessionSummaryFilters,
  type AgentSessionSummaryListResponse,
  type AgentSessionPreview,
  type AgentFiltersResponse,
  type RecallFilters,
  type RecallResponse,
  type RecallContextResponse,
} from "./index";

/**
 * Hook to fetch sessions for the timeline page.
 */
type AgentSessionsQueryOptions = Pick<
  UseQueryOptions<TimelineSessionsListResponse>,
  "enabled" | "refetchInterval"
>;

export function useAgentSessions(
  filters: AgentSessionFilters = {},
  options: AgentSessionsQueryOptions = {}
) {
  return useQuery<TimelineSessionsListResponse>({
    queryKey: ["agent-sessions", filters],
    queryFn: () => fetchAgentSessions(filters),
    meta: { apiHealth: true },
    enabled: options.enabled !== false,
    refetchInterval: options.refetchInterval,
    staleTime: 30_000,
    gcTime: 5 * 60_000,
    placeholderData: keepPreviousData,
  });
}

type AgentSessionWorkspaceQueryOptions = Pick<
  UseQueryOptions<AgentSessionWorkspaceResponse>,
  "enabled" | "refetchInterval"
>;

type AgentSessionWorkspaceParams = {
  limit?: number;
  branch_mode?: "head" | "all";
  shared_by?: number | null;
  share_token?: string | null;
};

/**
 * The one query key and fetch for a session workspace. Prefetchers and the
 * session page both build from this, so a prefetch lands in the cache entry
 * the page reads (an `undefined` and a `null` share param hash differently).
 */
export function agentSessionWorkspaceQueryOptions(
  sessionId: string | null,
  { limit = 200, branch_mode = "head", shared_by, share_token }: AgentSessionWorkspaceParams = {},
) {
  const params = { limit, branch_mode, shared_by: shared_by ?? null, share_token: share_token ?? null };
  return {
    queryKey: ["agent-session-workspace", sessionId, params] as const,
    queryFn: () => fetchAgentSessionWorkspace(sessionId!, params),
    staleTime: 10_000,
    gcTime: 5 * 60_000,
  };
}

export function useAgentSessionWorkspace(
  sessionId: string | null,
  options: AgentSessionWorkspaceQueryOptions & AgentSessionWorkspaceParams = {},
) {
  const { enabled, refetchInterval, ...params } = options;

  return useQuery<AgentSessionWorkspaceResponse>({
    ...agentSessionWorkspaceQueryOptions(sessionId, params),
    enabled: enabled ?? !!sessionId,
    refetchInterval,
  });
}

export function useAgentSessionProjectionInfinite(
  sessionId: string | null,
  options: {
    limit?: number;
    enabled?: boolean;
    branch_mode?: "head" | "all";
    initialPage?: AgentSessionProjectionResponse | null;
    refetchInterval?: number | false;
  } = {}
) {
  const { limit = 1000, enabled = true, branch_mode = "head", initialPage = null, refetchInterval } = options;
  type ProjectionPageParam =
    | { anchor: "tail"; cursor?: string }
    | { anchor: "start"; offset: number; limit: number };

  return useInfiniteQuery<
    AgentSessionProjectionResponse,
    Error,
    InfiniteData<AgentSessionProjectionResponse>,
    unknown[],
    ProjectionPageParam
  >({
    queryKey: ["agent-session-projection-infinite", sessionId, { limit, branch_mode }],
    queryFn: ({ pageParam }) =>
      fetchAgentSessionProjection(sessionId!, {
        limit: pageParam.anchor === "tail" ? limit : pageParam.limit,
        anchor: pageParam.anchor,
        cursor: pageParam.anchor === "tail" ? pageParam.cursor : undefined,
        offset: pageParam.anchor === "start" ? pageParam.offset : undefined,
        branch_mode,
      }),
    initialPageParam: { anchor: "tail" },
    // The initial page is the latest tail window. "Previous" pages are older
    // slices prepended above it in display order.
    getPreviousPageParam: (firstPage) => {
      if (firstPage.generation_id) {
        return firstPage.has_more && firstPage.next_cursor
          ? { anchor: "tail", cursor: firstPage.next_cursor }
          : undefined;
      }
      const currentOffset = firstPage.page_offset ?? 0;
      if (currentOffset <= 0) return undefined;

      const previousLimit = Math.min(limit, currentOffset);
      return {
        anchor: "start",
        offset: currentOffset - previousLimit,
        limit: previousLimit,
      };
    },
    getNextPageParam: () => undefined,
    enabled: !!sessionId && enabled,
    // Keep the seed page anchored to the tail so refetches continue to track
    // newly appended events after the session grows beyond one page.
    initialData: initialPage
      ? { pages: [initialPage], pageParams: [{ anchor: "tail" }] }
      : undefined,
    // A built-in refetch restarts from the oldest loaded page and drops the
    // rest (see refreshAgentSessionProjectionTail), so once the reader has
    // scrolled up, only refreshAgentSessionProjectionTail may refresh it.
    refetchInterval: (query) => (holdsOlderPages(query.state.data) ? false : (refetchInterval ?? false)),
    refetchOnMount: (query) => !holdsOlderPages(query.state.data),
    refetchOnWindowFocus: (query) => !holdsOlderPages(query.state.data),
    refetchOnReconnect: (query) => !holdsOlderPages(query.state.data),
    staleTime: 10_000,
    gcTime: 5 * 60_000,
  });
}

type ProjectionInfiniteKey = [
  "agent-session-projection-infinite",
  string,
  { limit: number; branch_mode: "head" | "all" },
];

export function holdsOlderPages(data: InfiniteData<AgentSessionProjectionResponse> | undefined): boolean {
  return (data?.pages.length ?? 0) > 1;
}

function projectionItemKey(item: AgentSessionProjectionResponse["items"][number]): string {
  if (item.kind === "event" && item.event) return `event:${item.event.id}`;
  if (item.kind === "action" && item.action) return `action:${item.action.id}`;
  return `seam:${item.session_id}:${item.timestamp}`;
}

/**
 * Replaces the newest window of an already-paged transcript, keeping the older
 * pages the reader scrolled up through. Returns null when the old pages can't
 * be stitched to the new tail (another generation or head session, or the
 * window moved past the old tail entirely), which means: start over from the
 * tail.
 */
export function stitchProjectionTail(
  data: InfiniteData<AgentSessionProjectionResponse>,
  freshTail: AgentSessionProjectionResponse,
): InfiniteData<AgentSessionProjectionResponse> | null {
  const oldTail = data.pages[data.pages.length - 1];
  if (
    !oldTail ||
    oldTail.generation_id !== freshTail.generation_id ||
    oldTail.head_session_id !== freshTail.head_session_id
  ) {
    return null;
  }
  const firstFreshKey = freshTail.items[0] ? projectionItemKey(freshTail.items[0]) : null;
  const overlapAt = firstFreshKey
    ? oldTail.items.findIndex((item) => projectionItemKey(item) === firstFreshKey)
    : -1;
  if (overlapAt < 0) return null;

  // Rows that slid out of the window stay as history; rows still in it come
  // from the fresh page so their state (a tool call finishing) updates.
  const slidOut = oldTail.items.slice(0, overlapAt);
  const olderPages = data.pages.slice(0, -1);
  const olderParams = data.pageParams.slice(0, -1);
  return {
    pages: slidOut.length
      ? [...olderPages, { ...oldTail, items: slidOut }, freshTail]
      : [...olderPages, freshTail],
    pageParams: slidOut.length
      ? [...olderParams, data.pageParams[data.pageParams.length - 1], { anchor: "tail" }]
      : [...olderParams, { anchor: "tail" }],
  };
}

function projectionInfiniteQueries(queryClient: QueryClient, sessionId: string) {
  return queryClient
    .getQueryCache()
    .findAll({ queryKey: ["agent-session-projection-infinite", sessionId] });
}

export function sessionHoldsOlderProjectionPages(queryClient: QueryClient, sessionId: string): boolean {
  return projectionInfiniteQueries(queryClient, sessionId).some((query) =>
    holdsOlderPages(query.state.data as InfiniteData<AgentSessionProjectionResponse> | undefined),
  );
}

/**
 * Brings a session's transcript up to date after a wake or a send.
 *
 * A plain invalidation refetches an infinite query from its first stored page
 * param, which here is the oldest page the reader scrolled up to, and then
 * stops (there is no "next" page). So after scrolling up, a wake used to throw
 * away the live tail and every other page. A query holding one page is still
 * invalidated normally; one holding older pages refetches only the tail.
 */
export async function refreshAgentSessionProjectionTail(
  queryClient: QueryClient,
  sessionId: string,
): Promise<void> {
  await Promise.all(
    projectionInfiniteQueries(queryClient, sessionId).map(async (query) => {
      const queryKey = query.queryKey as ProjectionInfiniteKey;
      const data = query.state.data as InfiniteData<AgentSessionProjectionResponse> | undefined;
      if (!holdsOlderPages(data)) {
        await queryClient.invalidateQueries({ queryKey, exact: true }, { cancelRefetch: false });
        return;
      }
      const { limit, branch_mode } = queryKey[2];
      const freshTail = await fetchAgentSessionProjection(sessionId, {
        limit,
        anchor: "tail",
        branch_mode,
      });
      queryClient.setQueryData<InfiniteData<AgentSessionProjectionResponse>>(queryKey, (current) => {
        if (!current) return current;
        return (
          stitchProjectionTail(current, freshTail) ?? {
            pages: [freshTail],
            pageParams: [{ anchor: "tail" }],
          }
        );
      });
    }),
  );
}

/**
 * Hook to fetch and search session summaries.
 */
export function useAgentSessionSummaries(
  filters: AgentSessionSummaryFilters = {},
  options: { enabled?: boolean } = {}
) {
  return useQuery<AgentSessionSummaryListResponse>({
    queryKey: ["agent-session-summaries", filters],
    queryFn: () => fetchAgentSessionSummaries(filters),
    enabled: options.enabled !== false,
    staleTime: 30_000,
    gcTime: 5 * 60_000,
  });
}

/**
 * Hook to preview a session's recent messages.
 */
export function useAgentSessionPreview(sessionId: string | null, lastN: number = 6) {
  return useQuery<AgentSessionPreview>({
    queryKey: ["agent-session-preview", sessionId, lastN],
    queryFn: () => fetchAgentSessionPreview(sessionId!, lastN),
    enabled: !!sessionId,
    staleTime: 60_000,
    gcTime: 10 * 60_000,
  });
}

/**
 * Hook to fetch distinct filters for sessions.
 */
export function useAgentSessionFilters(daysBack: number = 90, enabled: boolean = true, includeHidden: boolean = false) {
  return useQuery<AgentFiltersResponse>({
    queryKey: ["agent-session-filters", daysBack, includeHidden],
    queryFn: () => fetchAgentFilters(daysBack, includeHidden),
    enabled,
    staleTime: 5 * 60_000,
    gcTime: 10 * 60_000,
  });
}

/**
 * Hook to fetch distinct filter values (alias for timeline usage).
 */
export function useAgentFilters(daysBack: number = 90, enabled: boolean = true, includeHidden: boolean = false) {
  return useAgentSessionFilters(daysBack, enabled, includeHidden);
}

/**
 * Hook for recall (turn-level semantic search with context).
 */
export function useRecall(
  filters: RecallFilters,
  options: { enabled?: boolean } = {}
) {
  return useQuery<RecallResponse>({
    queryKey: ["recall", filters],
    queryFn: () => fetchRecall(filters),
    enabled: options.enabled !== false && !!filters.query,
    staleTime: 60_000,
    gcTime: 5 * 60_000,
  });
}

/** Fetch one bounded recall expansion only after the user opens its card. */
export function useRecallContext(ref: string | null) {
  return useQuery<RecallContextResponse>({
    queryKey: ["recall-context", ref],
    queryFn: () => fetchRecallContext(ref!),
    enabled: !!ref,
    staleTime: 5 * 60_000,
    gcTime: 10 * 60_000,
  });
}
