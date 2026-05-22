import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  connectTimelineSessionsStream,
  type AgentSessionFilters,
  getTimelineCardAnchor,
  type TimelineSessionCard,
  type TimelineSessionsListResponse,
  type TimelineSessionRemoveEvent,
  type TimelineSessionUpsertEvent,
} from "../services/api/agents";

function sessionAnchorMillis(card: TimelineSessionCard): number {
  return new Date(getTimelineCardAnchor(card)).getTime();
}

function sortSessionsByAnchor(sessions: TimelineSessionCard[]): TimelineSessionCard[] {
  return [...sessions].sort((a, b) => sessionAnchorMillis(b) - sessionAnchorMillis(a));
}

function upsertTimelineSession(
  current: TimelineSessionsListResponse,
  event: TimelineSessionUpsertEvent,
  limit: number | undefined,
): TimelineSessionsListResponse {
  const sessions = sortSessionsByAnchor([
    event.session,
    ...current.sessions.filter((session) => session.thread_id !== event.session.thread_id),
  ]);

  return {
    sessions: typeof limit === "number" ? sessions.slice(0, limit) : sessions,
    total: event.total ?? current.total,
    has_real_sessions: event.has_real_sessions ?? current.has_real_sessions,
  };
}

function removeTimelineSession(
  current: TimelineSessionsListResponse,
  event: TimelineSessionRemoveEvent,
): TimelineSessionsListResponse {
  return {
    total: event.total ?? current.total,
    has_real_sessions: event.has_real_sessions ?? current.has_real_sessions,
    sessions: current.sessions.filter((session) => session.thread_id !== event.thread_id),
  };
}

export interface UseTimelineSessionStreamOptions {
  enabled?: boolean;
  skipInitialReplay?: boolean;
}

export function useTimelineSessionStream(
  filters: AgentSessionFilters,
  options: UseTimelineSessionStreamOptions = {},
) {
  const queryClient = useQueryClient();
  const enabled = options.enabled !== false;
  const skipInitialReplay = options.skipInitialReplay === true;
  const skipInitialReplayRef = useRef(skipInitialReplay);
  skipInitialReplayRef.current = skipInitialReplay;

  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") {
      return;
    }

    return connectTimelineSessionsStream(
      filters,
      {
        onSessionUpsert: (event) => {
          queryClient.setQueryData<TimelineSessionsListResponse>(
            ["agent-sessions", filters],
            (current) => (current ? upsertTimelineSession(current, event, filters.limit) : current),
          );
        },
        onSessionRemove: (event) => {
          queryClient.setQueryData<TimelineSessionsListResponse>(
            ["agent-sessions", filters],
            (current) => (current ? removeTimelineSession(current, event) : current),
          );
        },
      },
      { skipInitialReplay: skipInitialReplayRef.current },
    );
  }, [enabled, filters, queryClient]);
}
