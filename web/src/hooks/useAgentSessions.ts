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
  type UseQueryOptions,
} from "@tanstack/react-query";
import {
  fetchAgentSessions,
  fetchAgentSession,
  fetchAgentSessionThread,
  fetchAgentSessionProjection,
  fetchAgentSessionWorkspace,
  fetchAgentSessionTurns,
  fetchAgentSessionEvents,
  fetchAgentSessionSummaries,
  fetchAgentSessionPreview,
  fetchAgentFilters,
  fetchRecall,
  type AgentSessionFilters,
  type TimelineSessionsListResponse,
  type AgentSession,
  type AgentSessionThreadResponse,
  type AgentSessionProjectionResponse,
  type AgentSessionWorkspaceResponse,
  type AgentSessionTurnsListResponse,
  type AgentEventsListResponse,
  type AgentSessionSummaryFilters,
  type AgentSessionSummaryListResponse,
  type AgentSessionPreview,
  type AgentFiltersResponse,
  type RecallFilters,
  type RecallResponse,
} from "../services/api";

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

/**
 * Hook to fetch a single session by ID.
 */
export function useAgentSession(sessionId: string | null) {
  return useAgentSessionWithOptions(sessionId);
}

type AgentSessionQueryOptions = Pick<
  UseQueryOptions<AgentSession>,
  "enabled" | "refetchInterval"
>;

export function useAgentSessionWithOptions(
  sessionId: string | null,
  options: AgentSessionQueryOptions = {},
) {
  return useQuery<AgentSession>({
    queryKey: ["agent-session", sessionId],
    queryFn: () => fetchAgentSession(sessionId!),
    enabled: options.enabled ?? !!sessionId,
    refetchInterval: options.refetchInterval,
    staleTime: 30_000,
    gcTime: 5 * 60_000,
  });
}

export function useAgentSessionThread(sessionId: string | null) {
  return useAgentSessionThreadWithOptions(sessionId);
}

type AgentSessionThreadQueryOptions = Pick<
  UseQueryOptions<AgentSessionThreadResponse>,
  "enabled" | "refetchInterval"
>;

export function useAgentSessionThreadWithOptions(
  sessionId: string | null,
  options: AgentSessionThreadQueryOptions = {},
) {
  return useQuery<AgentSessionThreadResponse>({
    queryKey: ["agent-session-thread", sessionId],
    queryFn: () => fetchAgentSessionThread(sessionId!),
    enabled: options.enabled ?? !!sessionId,
    refetchInterval: options.refetchInterval,
    staleTime: 30_000,
    gcTime: 5 * 60_000,
  });
}

type AgentSessionWorkspaceQueryOptions = Pick<
  UseQueryOptions<AgentSessionWorkspaceResponse>,
  "enabled" | "refetchInterval"
>;

export function useAgentSessionWorkspace(
  sessionId: string | null,
  options: AgentSessionWorkspaceQueryOptions & {
    limit?: number;
    branch_mode?: "head" | "all";
    shared_by?: number | null;
    share_token?: string | null;
  } = {},
) {
  const {
    limit = 200,
    branch_mode = "head",
    shared_by,
    share_token,
    enabled,
    refetchInterval,
  } = options;

  return useQuery<AgentSessionWorkspaceResponse>({
    queryKey: ["agent-session-workspace", sessionId, { limit, branch_mode, shared_by, share_token }],
    queryFn: () =>
      fetchAgentSessionWorkspace(sessionId!, {
        limit,
        branch_mode,
        shared_by,
        share_token,
      }),
    enabled: enabled ?? !!sessionId,
    refetchInterval,
    staleTime: 10_000,
    gcTime: 5 * 60_000,
  });
}

type AgentSessionTurnsQueryOptions = Pick<
  UseQueryOptions<AgentSessionTurnsListResponse>,
  "enabled" | "refetchInterval"
>;

export function useAgentSessionTurns(
  sessionId: string | null,
  options: AgentSessionTurnsQueryOptions & {
    limit?: number;
    offset?: number;
    order?: "asc" | "desc";
  } = {},
) {
  const {
    limit = 10,
    offset = 0,
    order = "desc",
    enabled,
    refetchInterval,
  } = options;

  return useQuery<AgentSessionTurnsListResponse>({
    queryKey: ["agent-session-turns", sessionId, { limit, offset, order }],
    queryFn: () =>
      fetchAgentSessionTurns(sessionId!, {
        limit,
        offset,
        order,
      }),
    enabled: enabled ?? !!sessionId,
    refetchInterval,
    staleTime: 10_000,
    gcTime: 5 * 60_000,
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
    refetchInterval,
    staleTime: 10_000,
    gcTime: 5 * 60_000,
  });
}

/**
 * Hook to fetch events for a session.
 */
export function useAgentSessionEvents(
  sessionId: string | null,
  options: { roles?: string; limit?: number; offset?: number; branch_mode?: "head" | "all" } = {}
) {
  return useQuery<AgentEventsListResponse>({
    queryKey: ["agent-session-events", sessionId, options],
    queryFn: () => fetchAgentSessionEvents(sessionId!, options),
    enabled: !!sessionId,
    staleTime: 10_000,
    gcTime: 5 * 60_000,
  });
}

/**
 * Hook to fetch paginated events for a session.
 *
 * Uses offset pagination under the hood and flattens pages in the caller.
 */
export function useAgentSessionEventsInfinite(
  sessionId: string | null,
  options: { roles?: string; limit?: number; enabled?: boolean; branch_mode?: "head" | "all" } = {}
) {
  const { roles, limit = 1000, enabled = true, branch_mode = "head" } = options;

  return useInfiniteQuery<AgentEventsListResponse>({
    queryKey: ["agent-session-events-infinite", sessionId, { roles, limit, branch_mode }],
    queryFn: ({ pageParam = 0 }) =>
      fetchAgentSessionEvents(sessionId!, {
        roles,
        limit,
        offset: Number(pageParam),
        branch_mode,
      }),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => {
      const loaded = pages.reduce((sum, page) => sum + page.events.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
    enabled: !!sessionId && enabled,
    staleTime: 10_000,
    gcTime: 5 * 60_000,
  });
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
export function useAgentSessionFilters(daysBack: number = 90, enabled: boolean = true) {
  return useQuery<AgentFiltersResponse>({
    queryKey: ["agent-session-filters", daysBack],
    queryFn: () => fetchAgentFilters(daysBack),
    enabled,
    staleTime: 5 * 60_000,
    gcTime: 10 * 60_000,
  });
}

/**
 * Hook to fetch distinct filter values (alias for timeline usage).
 */
export function useAgentFilters(daysBack: number = 90, enabled: boolean = true) {
  return useAgentSessionFilters(daysBack, enabled);
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
