import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  connectTimelineSessionsStream,
  type AgentSession,
  type AgentSessionFilters,
  type AgentSessionsListResponse,
  type TimelineSessionRemoveEvent,
  type TimelineSessionUpsertEvent,
} from "../services/api/agents";

function sessionAnchorMillis(session: Pick<AgentSession, "timeline_anchor_at" | "last_activity_at" | "started_at">): number {
  return new Date(session.timeline_anchor_at || session.last_activity_at || session.started_at).getTime();
}

function sortSessionsByAnchor(sessions: AgentSession[]): AgentSession[] {
  return [...sessions].sort((a, b) => sessionAnchorMillis(b) - sessionAnchorMillis(a));
}

function upsertTimelineSession(
  current: AgentSessionsListResponse,
  event: TimelineSessionUpsertEvent,
  limit: number | undefined,
): AgentSessionsListResponse {
  const sessions = sortSessionsByAnchor([
    event.session,
    ...current.sessions.filter((session) => session.id !== event.session.id),
  ]);

  return {
    sessions: typeof limit === "number" ? sessions.slice(0, limit) : sessions,
    total: event.total ?? current.total,
    has_real_sessions: event.has_real_sessions ?? current.has_real_sessions,
  };
}

function removeTimelineSession(
  current: AgentSessionsListResponse,
  event: TimelineSessionRemoveEvent,
): AgentSessionsListResponse {
  return {
    total: event.total ?? current.total,
    has_real_sessions: event.has_real_sessions ?? current.has_real_sessions,
    sessions: current.sessions.filter((session) => session.id !== event.session_id),
  };
}

export interface UseTimelineSessionStreamOptions {
  enabled?: boolean;
}

export function useTimelineSessionStream(
  filters: AgentSessionFilters,
  options: UseTimelineSessionStreamOptions = {},
) {
  const queryClient = useQueryClient();
  const enabled = options.enabled !== false;

  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") {
      return;
    }

    return connectTimelineSessionsStream(filters, {
      onSessionUpsert: (event) => {
        queryClient.setQueryData<AgentSessionsListResponse>(
          ["agent-sessions", filters],
          (current) => (current ? upsertTimelineSession(current, event, filters.limit) : current),
        );
      },
      onSessionRemove: (event) => {
        queryClient.setQueryData<AgentSessionsListResponse>(
          ["agent-sessions", filters],
          (current) => (current ? removeTimelineSession(current, event) : current),
        );
      },
    });
  }, [enabled, filters, queryClient]);
}
