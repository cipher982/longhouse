import { useQuery } from "@tanstack/react-query";
import {
  fetchAgentActiveSessions,
  type AgentActiveSession,
  type AgentActiveSessionsResponse,
  type AgentAttentionLevel,
  type AgentSessionStatus,
} from "../services/api";

export type SessionStatus = AgentSessionStatus;
export type AttentionLevel = AgentAttentionLevel;
export type ActiveSession = AgentActiveSession;
export type ActiveSessionsResponse = AgentActiveSessionsResponse;

export interface UseActiveSessionsOptions {
  project?: string;
  attention?: AttentionLevel;
  status?: SessionStatus;
  limit?: number;
  pollInterval?: number;
  enabled?: boolean;
  days_back?: number;
}

/**
 * Hook to fetch active sessions from the materialized view.
 * Used by Forum UI to display real-time session state.
 */
export function useActiveSessions(options: UseActiveSessionsOptions = {}) {
  const {
    project,
    attention,
    status,
    limit = 50,
    pollInterval = 10000,
    enabled = true,
    days_back,
  } = options;

  return useQuery({
    queryKey: ["active-sessions", { project, attention, status, limit, days_back }],
    queryFn: async () => {
      return fetchAgentActiveSessions({
        project,
        attention,
        status,
        limit,
        days_back,
      });
    },
    refetchInterval: pollInterval,
    enabled,
    staleTime: 5000,
  });
}
