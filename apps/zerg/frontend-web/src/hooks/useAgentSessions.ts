/**
 * React Query hooks for agent session data.
 *
 * Used by the Sessions Timeline pages to fetch and display past AI sessions.
 */

import { useQuery } from "@tanstack/react-query";
import {
  fetchAgentSessions,
  fetchAgentSession,
  fetchAgentSessionEvents,
  fetchAgentFilters,
  type AgentSessionFilters,
  type AgentSessionsListResponse,
  type AgentSession,
  type AgentEventsListResponse,
  type AgentFiltersResponse,
} from "../services/api/agents";

/**
 * Hook to fetch and search agent sessions.
 *
 * @param filters - Optional filters for project, provider, query, etc.
 * @param options - Query options (enabled, refetchInterval)
 */
export function useAgentSessions(
  filters: AgentSessionFilters = {},
  options: { enabled?: boolean; refetchInterval?: number | false } = {}
) {
  return useQuery<AgentSessionsListResponse>({
    queryKey: ["agent-sessions", filters],
    queryFn: () => fetchAgentSessions(filters),
    enabled: options.enabled !== false,
    staleTime: 30_000, // Consider data fresh for 30s
    gcTime: 5 * 60_000, // Keep in cache for 5 minutes
    refetchInterval: options.refetchInterval,
  });
}

/**
 * Hook to fetch a single session by ID.
 *
 * @param sessionId - Session UUID (null to disable)
 */
export function useAgentSession(sessionId: string | null) {
  return useQuery<AgentSession>({
    queryKey: ["agent-session", sessionId],
    queryFn: () => fetchAgentSession(sessionId!),
    enabled: !!sessionId,
    staleTime: 60_000,
    gcTime: 10 * 60_000,
  });
}

/**
 * Hook to fetch events for a session.
 *
 * @param sessionId - Session UUID (null to disable)
 * @param options - Fetch options (roles filter, limit, offset)
 */
export function useAgentSessionEvents(
  sessionId: string | null,
  options: { roles?: string; limit?: number; offset?: number } = {}
) {
  return useQuery<AgentEventsListResponse>({
    queryKey: ["agent-session-events", sessionId, options],
    queryFn: () => fetchAgentSessionEvents(sessionId!, options),
    enabled: !!sessionId,
    staleTime: 60_000,
    gcTime: 10 * 60_000,
  });
}

/**
 * Hook to fetch distinct filter values for dropdowns.
 *
 * @param daysBack - Days to look back for distinct values
 */
export function useAgentFilters(daysBack: number = 90) {
  return useQuery<AgentFiltersResponse>({
    queryKey: ["agent-filters", daysBack],
    queryFn: () => fetchAgentFilters(daysBack),
    staleTime: 5 * 60_000, // Cache for 5 minutes
    gcTime: 30 * 60_000,
  });
}
