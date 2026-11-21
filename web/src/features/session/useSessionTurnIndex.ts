import { useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchSessionTurnIndex, type SessionTurn } from "@/shared/api/agents";
import type { TurnOutlineTurn } from "./TurnOutline";

export const SESSION_TURN_INDEX_QUERY_KEY = "agent-session-turn-index";
/** How many of the newest turns the outline lists. */
export const SESSION_TURN_OUTLINE_LIMIT = 200;
/** Historical turns outside the loaded window do not change; live and rewound
 * turns inside it merge immediately and trigger a one-shot refetch. Keeping
 * cached responses fresh for 5 minutes avoids re-fetching when switching back
 * and forth between sessions. */
const SESSION_TURN_INDEX_STALE_MS = 5 * 60_000;

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
  const query = useQuery({
    queryKey: [SESSION_TURN_INDEX_QUERY_KEY, sessionId, branchMode] as const,
    queryFn: () =>
      fetchSessionTurnIndex(sessionId!, { branch_mode: branchMode, limit: SESSION_TURN_OUTLINE_LIMIT }),
    enabled: Boolean(sessionId),
    staleTime: SESSION_TURN_INDEX_STALE_MS,
    refetchOnWindowFocus: false,
    retry: 1,
  });
  const indexed = query.data?.turns ?? null;

  const unindexedKey = useMemo(() => {
    if (!indexed) return "";
    const known = new Set(indexed.map((turn) => turn.event_id));
    const missing = loadedTurns
      .filter((turn) => !known.has(String(turn.eventId)))
      .map((turn) => String(turn.eventId))
      .join(",");
    return missing ? `${sessionId ?? ""}:${branchMode}:${missing}` : "";
  }, [indexed, loadedTurns, sessionId, branchMode]);

  const refetchedFor = useRef("");
  const { refetch, isFetching } = query;
  useEffect(() => {
    if (!unindexedKey || isFetching || refetchedFor.current === unindexedKey) return;
    refetchedFor.current = unindexedKey;
    void refetch();
  }, [unindexedKey, isFetching, refetch]);

  return indexed;
}
