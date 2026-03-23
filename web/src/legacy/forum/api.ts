import { useQuery } from "@tanstack/react-query";
import { request } from "../../services/api/base";
import type {
  AgentSessionStatus,
  ManagedSessionTransport,
  PresenceState,
  SessionExecutionHome,
  SessionLoopMode,
} from "../../services/api/agents";

export type ForumAttentionLevel = "hard" | "needs" | "soft" | "auto";

export interface ForumActiveSession {
  id: string;
  project: string | null;
  provider: string;
  cwd: string | null;
  git_repo?: string | null;
  git_branch: string | null;
  started_at: string;
  ended_at: string | null;
  last_activity_at: string;
  timeline_anchor_at?: string;
  runtime_phase?: string | null;
  phase_started_at?: string | null;
  last_progress_at?: string | null;
  runtime_source?: string | null;
  terminal_state?: string | null;
  runtime_version?: number | null;
  status: AgentSessionStatus;
  attention: ForumAttentionLevel;
  duration_minutes: number;
  last_user_message: string | null;
  last_assistant_message: string | null;
  message_count: number;
  tool_calls: number;
  presence_state: PresenceState | null;
  presence_tool: string | null;
  presence_updated_at?: string | null;
  last_live_at?: string | null;
  display_phase?: string | null;
  active_tool?: string | null;
  confidence?: string | null;
  user_state: "active" | "parked" | "snoozed" | "archived";
  execution_home?: SessionExecutionHome;
  managed_transport?: ManagedSessionTransport | null;
  source_runner_id?: number | null;
  source_runner_name?: string | null;
  loop_mode?: SessionLoopMode;
}

export interface ForumActiveSessionsResponse {
  sessions: ForumActiveSession[];
  total: number;
  last_refresh: string;
}

export interface UseForumSessionsOptions {
  project?: string;
  attention?: ForumAttentionLevel;
  status?: AgentSessionStatus;
  limit?: number;
  pollInterval?: number;
  enabled?: boolean;
  days_back?: number;
}

async function fetchForumSessions(
  filters: Omit<UseForumSessionsOptions, "pollInterval" | "enabled"> = {},
): Promise<ForumActiveSessionsResponse> {
  const params = new URLSearchParams();

  if (filters.project) params.set("project", filters.project);
  if (filters.attention) params.set("attention", filters.attention);
  if (filters.status) params.set("status", filters.status);
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.days_back) params.set("days_back", String(filters.days_back));

  const queryString = params.toString();
  const path = `/timeline/sessions/active${queryString ? `?${queryString}` : ""}`;

  return request<ForumActiveSessionsResponse>(path, { method: "GET" });
}

export function useForumSessions(options: UseForumSessionsOptions = {}) {
  const {
    project,
    attention,
    status,
    limit = 50,
    pollInterval = 2000,
    enabled = true,
    days_back,
  } = options;

  return useQuery({
    queryKey: ["legacy-forum-sessions", { project, attention, status, limit, days_back }],
    queryFn: async () =>
      fetchForumSessions({
        project,
        attention,
        status,
        limit,
        days_back,
      }),
    refetchInterval: (query) => {
      if (query.state.error && (query.state.error as { status?: number }).status === 401) {
        return false;
      }
      return pollInterval;
    },
    retry: (failureCount, error) => {
      if ((error as { status?: number }).status === 401) return false;
      return failureCount < 2;
    },
    enabled,
    staleTime: 5000,
  });
}
