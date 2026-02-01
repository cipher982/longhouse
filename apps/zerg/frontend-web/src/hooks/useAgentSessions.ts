/**
 * React Query hooks for Longhouse agent session data.
 *
 * Used by the Session Picker modal and other session UIs.
 */

import { useQuery } from "@tanstack/react-query";
import {
  fetchAgentSessions,
  fetchAgentSession,
  fetchAgentSessionEvents,
  fetchAgentSessionSummaries,
  fetchAgentSessionPreview,
  fetchAgentFilters,
  type AgentSessionFilters,
  type AgentSessionsListResponse,
  type AgentSession,
  type AgentEventsListResponse,
  type AgentSessionSummaryFilters,
  type AgentSessionSummaryListResponse,
  type AgentSessionPreview,
  type AgentFiltersResponse,
} from "../services/api";

/**
 * Hook to fetch sessions for the timeline page.
 */
export function useAgentSessions(
  filters: AgentSessionFilters = {},
  options: { enabled?: boolean; refetchInterval?: number } = {}
) {
  return useQuery<AgentSessionsListResponse>({
    queryKey: ["agent-sessions", filters],
    queryFn: () => fetchAgentSessions(filters),
    enabled: options.enabled !== false,
    refetchInterval: options.refetchInterval,
    staleTime: 30_000,
    gcTime: 5 * 60_000,
  });
}

/**
 * Hook to fetch a single session by ID.
 */
export function useAgentSession(sessionId: string | null) {
  return useQuery<AgentSession>({
    queryKey: ["agent-session", sessionId],
    queryFn: () => fetchAgentSession(sessionId!),
    enabled: !!sessionId,
    staleTime: 30_000,
    gcTime: 5 * 60_000,
  });
}

/**
 * Hook to fetch events for a session.
 */
export function useAgentSessionEvents(
  sessionId: string | null,
  options: { roles?: string; limit?: number; offset?: number } = {}
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
