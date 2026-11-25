/**
 * Read-on-open acknowledgement for Console results
 * (control-plane/docs/specs/console-unread-acknowledgement.md).
 *
 * Fires POST /sessions/{id}/read with the result timestamp this client
 * actually rendered — acknowledgement is bounded to what was seen, and only
 * while the tab is visible: a restored background tab must not clear unread
 * it never showed. Re-fires when a turn settles while the workspace stays
 * open (last_result_at moves via the workspace stream).
 *
 * Once the server acknowledges the read, patches cached session lists and
 * workspaces immediately (only if their last_result_at is at or before
 * readThrough) and invalidates ["agent-sessions"] so the left rail and
 * Timeline drop the session out of Unread right away instead of waiting for
 * the rail's 30s poll.
 */

import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  markSessionRead,
  type AgentSessionWorkspaceResponse,
  type SessionStateFacts,
  type TimelineSessionsListResponse,
} from "@/shared/api/agents";

function isCoveredByReadThrough(
  candidateResultAt: string | null | undefined,
  readThrough: string,
): boolean {
  if (!candidateResultAt) return false;
  if (candidateResultAt === readThrough) return true;
  const candidateMs = Date.parse(candidateResultAt);
  const readThroughMs = Date.parse(readThrough);
  if (Number.isNaN(candidateMs) || Number.isNaN(readThroughMs)) return false;
  return candidateMs <= readThroughMs;
}

function clearUnreadInSessionsList(
  current: TimelineSessionsListResponse | undefined,
  sessionId: string,
  readThrough: string,
): TimelineSessionsListResponse | undefined {
  if (!current) return current;
  let changed = false;
  const sessions = current.sessions.map((card) => {
    if (card.head.id !== sessionId) return card;
    if (!card.head.session_state.unread) return card;
    if (!isCoveredByReadThrough(card.head.session_state.last_result_at, readThrough)) {
      return card;
    }
    changed = true;
    return {
      ...card,
      head: {
        ...card.head,
        session_state: {
          ...card.head.session_state,
          unread: false,
        },
      },
    };
  });
  return changed ? { ...current, sessions } : current;
}

function clearUnreadInWorkspace(
  current: AgentSessionWorkspaceResponse | undefined,
  readThrough: string,
): AgentSessionWorkspaceResponse | undefined {
  if (!current?.session.session_state.unread) return current;
  if (!isCoveredByReadThrough(current.session.session_state.last_result_at, readThrough)) {
    return current;
  }
  return {
    ...current,
    session: {
      ...current.session,
      session_state: {
        ...current.session.session_state,
        unread: false,
      },
    },
  };
}

export function useMarkSessionRead({
  sessionId,
  sessionState,
}: {
  sessionId: string | null;
  sessionState: SessionStateFacts | null | undefined;
}): void {
  const queryClient = useQueryClient();
  // Prevents duplicate POST /read if a stale refetch briefly flips unread back on for the same result.
  const ackedKeyRef = useRef<string | null>(null);
  const unread = sessionState?.unread === true;
  const readThrough = sessionState?.last_result_at ?? null;

  useEffect(() => {
    if (!sessionId || !unread || !readThrough) return;
    const key = `${sessionId}:${readThrough}`;
    const fire = (): boolean => {
      if (document.visibilityState !== "visible") return false;
      if (ackedKeyRef.current === key) return true;
      ackedKeyRef.current = key;
      void markSessionRead(sessionId, readThrough)
        .then(() => {
          queryClient.setQueriesData<TimelineSessionsListResponse>(
            { queryKey: ["agent-sessions"] },
            (current) => clearUnreadInSessionsList(current, sessionId, readThrough),
          );
          queryClient.setQueriesData<AgentSessionWorkspaceResponse>(
            { queryKey: ["agent-session-workspace", sessionId] },
            (current) => clearUnreadInWorkspace(current, readThrough),
          );
          void queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
        })
        .catch(() => {
          if (ackedKeyRef.current === key) {
            ackedKeyRef.current = null;
          }
        });
      return true;
    };
    if (fire()) return;
    const onVisible = () => {
      if (fire()) document.removeEventListener("visibilitychange", onVisible);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [sessionId, unread, readThrough, queryClient]);
}
