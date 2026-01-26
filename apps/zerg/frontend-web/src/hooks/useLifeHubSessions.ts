/**
 * React Query hooks for Life Hub session data.
 *
 * Used by the Session Picker modal to fetch and preview past AI sessions.
 */

import { useQuery } from "@tanstack/react-query";
import {
  fetchSessions,
  fetchSessionPreview,
  type SessionFilters,
  type SessionsListResponse,
  type SessionPreview,
} from "../services/api";

/**
 * Hook to fetch and search Life Hub sessions.
 *
 * @param filters - Optional filters for query, project, provider
 * @param options - Optional query options (enabled, etc.)
 */
export function useLifeHubSessions(
  filters: SessionFilters = {},
  options: { enabled?: boolean } = {}
) {
  return useQuery<SessionsListResponse>({
    queryKey: ["life-hub-sessions", filters],
    queryFn: () => fetchSessions(filters),
    enabled: options.enabled !== false,
    staleTime: 30_000, // Consider data fresh for 30s
    gcTime: 5 * 60_000, // Keep in cache for 5 minutes
  });
}

/**
 * Hook to preview a session's recent messages.
 *
 * @param sessionId - Session UUID to preview (null to disable)
 * @param lastN - Number of messages to fetch (default 6)
 */
export function useSessionPreview(
  sessionId: string | null,
  lastN: number = 6
) {
  return useQuery<SessionPreview>({
    queryKey: ["session-preview", sessionId, lastN],
    queryFn: () => fetchSessionPreview(sessionId!, lastN),
    enabled: !!sessionId,
    staleTime: 60_000, // Previews can be cached longer
    gcTime: 10 * 60_000,
  });
}
