import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { agentSessionWorkspaceQueryOptions } from "@/shared/api/useAgentSessions";

/** How many rail sessions get warmed: the ones a ⌃-number reaches. */
export const RAIL_PREFETCH_COUNT = 8;
/** The session page's first workspace page; the prefetch must land in the
 * same cache entry the page reads (see agentSessionWorkspaceQueryOptions). */
const RAIL_PREFETCH_LIMIT = 200;

type NetworkInformationLike = { saveData?: boolean; effectiveType?: string };

/** Data Saver or a 2G-class link: leave the bandwidth to what the user is
 * looking at (session-view-terminal-parity C6). */
export function railPrefetchAllowed(nav: Navigator | undefined = globalThis.navigator): boolean {
  const connection = (nav as (Navigator & { connection?: NetworkInformationLike }) | undefined)
    ?.connection;
  if (!connection) return true;
  if (connection.saveData) return false;
  return !["slow-2g", "2g"].includes(connection.effectiveType ?? "");
}

type IdleHandle = number;
const requestIdle: (callback: () => void) => IdleHandle =
  typeof window !== "undefined" && "requestIdleCallback" in window
    ? (callback) => window.requestIdleCallback(callback, { timeout: 4_000 })
    : (callback) => window.setTimeout(callback, 600);
const cancelIdle: (handle: IdleHandle) => void =
  typeof window !== "undefined" && "cancelIdleCallback" in window
    ? (handle) => window.cancelIdleCallback(handle)
    : (handle) => window.clearTimeout(handle);

/**
 * Warm the workspace of the rail's top sessions while the browser is idle,
 * one at a time, so switching sessions paints from cache. The open session is
 * skipped (its page is already loading it) and so is anything still fresh in
 * the cache.
 */
export function useRailPrefetch(sessionIds: readonly string[], activeSessionId: string | null) {
  const queryClient = useQueryClient();
  const key = sessionIds.slice(0, RAIL_PREFETCH_COUNT).join(",");

  useEffect(() => {
    if (!key || !railPrefetchAllowed()) return;
    const queue = key.split(",").filter((id) => id && id !== activeSessionId);
    let cancelled = false;
    let handle: IdleHandle | null = null;

    const next = () => {
      if (cancelled) return;
      const sessionId = queue.shift();
      if (!sessionId) return;
      handle = requestIdle(() => {
        handle = null;
        if (cancelled) return;
        void queryClient
          .prefetchQuery(agentSessionWorkspaceQueryOptions(sessionId, { limit: RAIL_PREFETCH_LIMIT }))
          .finally(next);
      });
    };
    next();

    return () => {
      cancelled = true;
      if (handle != null) cancelIdle(handle);
    };
  }, [key, activeSessionId, queryClient]);
}
