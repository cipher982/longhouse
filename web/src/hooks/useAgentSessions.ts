/**
 * React Query hooks for Longhouse agent session data.
 *
 * Used by the Session Picker modal and other session UIs.
 */

import { useInfiniteQuery, useQuery, keepPreviousData, type UseQueryOptions } from "@tanstack/react-query";
import {
  fetchAgentSessions,
  fetchAgentSession,
  fetchAgentSessionThread,
  fetchAgentSessionProjection,
  fetchAgentSessionWorkspace,
  fetchAgentSessionEvents,
  fetchAgentSessionSummaries,
  fetchAgentSessionPreview,
  fetchAgentFilters,
  fetchRecall,
  fetchAgentBriefing,
  type BriefingResponse,
  type AgentSessionFilters,
  type TimelineSessionsListResponse,
  type AgentSession,
  type AgentSessionThreadResponse,
  type AgentSessionProjectionResponse,
  type AgentSessionWorkspaceResponse,
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
  } = {},
) {
  const {
    limit = 200,
    branch_mode = "head",
    enabled,
    refetchInterval,
  } = options;

  return useQuery<AgentSessionWorkspaceResponse>({
    queryKey: ["agent-session-workspace", sessionId, { limit, branch_mode }],
    queryFn: () =>
      fetchAgentSessionWorkspace(sessionId!, {
        limit,
        branch_mode,
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
  } = {}
) {
  const { limit = 1000, enabled = true, branch_mode = "head", initialPage = null } = options;

  return useInfiniteQuery<AgentSessionProjectionResponse>({
    queryKey: ["agent-session-projection-infinite", sessionId, { limit, branch_mode }],
    queryFn: ({ pageParam = 0 }) =>
      fetchAgentSessionProjection(sessionId!, {
        limit,
        offset: Number(pageParam),
        branch_mode,
      }),
    initialPageParam: 0,
    // Backward pagination: each "next" page loads events that come before the
    // oldest page currently loaded. page_offset=0 means we've reached the start.
    getNextPageParam: (lastPage) => {
      const currentOffset = lastPage.page_offset ?? 0;
      return currentOffset > 0 ? Math.max(0, currentOffset - limit) : undefined;
    },
    enabled: !!sessionId && enabled,
    // Seed page params from page_offset so backward pagination works correctly.
    initialData: initialPage
      ? { pages: [initialPage], pageParams: [initialPage.page_offset ?? 0] }
      : undefined,
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

export function useBriefing(project: string, limit: number = 5) {
  return useQuery<BriefingResponse>({
    queryKey: ["briefing", project, limit],
    queryFn: () => fetchAgentBriefing(project, limit),
    enabled: !!project,
    staleTime: 2 * 60_000,
    gcTime: 10 * 60_000,
  });
}
