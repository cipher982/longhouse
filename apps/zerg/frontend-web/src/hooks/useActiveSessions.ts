import { useQuery } from "@tanstack/react-query";
import { request } from "../services/api";

export type SessionStatus = "working" | "thinking" | "idle" | "completed" | "active";
export type AttentionLevel = "hard" | "needs" | "soft" | "auto";

export interface ActiveSession {
  id: string;
  project: string | null;
  provider: string;
  cwd: string | null;
  git_branch: string | null;
  started_at: string;
  ended_at: string | null;
  last_activity_at: string;
  status: SessionStatus;
  attention: AttentionLevel;
  duration_minutes: number;
  last_user_message: string | null;
  last_assistant_message: string | null;
  message_count: number;
  tool_calls: number;
}

export interface ActiveSessionsResponse {
  sessions: ActiveSession[];
  total: number;
  last_refresh: string;
}

export interface UseActiveSessionsOptions {
  project?: string;
  attention?: AttentionLevel;
  status?: SessionStatus;
  limit?: number;
  pollInterval?: number;
  enabled?: boolean;
}

/**
 * Hook to fetch active sessions from the materialized view.
 * Used by Forum UI to display real-time session state.
 */
export function useActiveSessions(options: UseActiveSessionsOptions = {}) {
  const { project, attention, status, limit = 50, pollInterval = 10000, enabled = true } = options;

  return useQuery({
    queryKey: ["active-sessions", { project, attention, status, limit }],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (project) params.set("project", project);
      if (attention) params.set("attention", attention);
      if (status) params.set("status", status);
      params.set("limit", String(limit));

      const queryString = params.toString();
      const url = queryString ? `/oikos/life-hub/sessions/active?${queryString}` : "/oikos/life-hub/sessions/active";

      return request<ActiveSessionsResponse>(url);
    },
    refetchInterval: pollInterval,
    enabled,
    staleTime: 5000,
  });
}

/**
 * Hook to manually refresh the session summary materialized view.
 */
export function useRefreshSessions() {
  return useQuery({
    queryKey: ["refresh-sessions"],
    queryFn: () => request<{ status: string; timestamp: string }>("/oikos/life-hub/sessions/refresh", { method: "POST" }),
    enabled: false, // Only run when manually triggered
  });
}
