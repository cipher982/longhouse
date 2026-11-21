import { useEffect, useMemo, useRef } from "react";
import { queryOptions, useQuery } from "@tanstack/react-query";
import { fetchSessionTurnIndex, type SessionTurn } from "@/shared/api/agents";
import type { TurnOutlineTurn } from "./TurnOutline";

export const SESSION_TURN_INDEX_QUERY_KEY = "agent-session-turn-index";
/** How many of the newest turns the outline lists. */
export const SESSION_TURN_OUTLINE_LIMIT = 200;
/** Historical turns outside the loaded window rarely change, and live/rewound
 * turns inside it are merged immediately and trigger a one-shot refetch. Keep
 * the index warm across session switches for 5 minutes. */
export const SESSION_TURN_INDEX_STALE_MS = 5 * 60_000;

export function sessionTurnIndexQueryOptions(
  sessionId: string | null,
  branchMode: "head" | "all" = "head",
) {
  return queryOptions({
    queryKey: [SESSION_TURN_INDEX_QUERY_KEY, sessionId, branchMode] as const,
    queryFn: () =>
      fetchSessionTurnIndex(sessionId!, { branch_mode: branchMode, limit: SESSION_TURN_OUTLINE_LIMIT }),
    enabled: Boolean(sessionId),
    staleTime: SESSION_TURN_INDEX_STALE_MS,
    refetchOnWindowFocus: false,
    retry: 1,
  });
}

/**
 * Every user turn of the session from the server's turn index, so the outline
 * covers history the transcript has not loaded.
 *
 * The index is refetched whenever the loaded thread shows a turn it does not
 * list yet (a new turn, or one created by a rewind), once per such set of
 * turns. Errors leave the outline on the loaded thread alone.
 */
export function useSessionTurnIndex(
  sessionId: string | null,
  branchMode: "head" | "all",
  loadedTurns: TurnOutlineTurn[],
): readonly SessionTurn[] | null {
  const query = useQuery(sessionTurnIndexQueryOptions(sessionId, branchMode));
  const indexed = query.data?.turns ?? null;

  const unindexedKey = useMemo(() => {
    if (!indexed) return "";
    const known = new Set(indexed.map((turn) => turn.event_id));
    return loadedTurns
      .filter((turn) => !known.has(String(turn.eventId)))
      .map((turn) => String(turn.eventId))
      .join(",");
  }, [indexed, loadedTurns]);

  const refetchedFor = useRef("");
  useEffect(() => {
    refetchedFor.current = "";
  }, [sessionId, branchMode]);

  const { refetch, isFetching } = query;
  useEffect(() => {
    if (!unindexedKey || isFetching || refetchedFor.current === unindexedKey) return;
    refetchedFor.current = unindexedKey;
    void refetch();
  }, [unindexedKey, isFetching, refetch]);

  return indexed;
}
