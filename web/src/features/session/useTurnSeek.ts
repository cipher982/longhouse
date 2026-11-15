import { useCallback, useEffect, useMemo, useState } from "react";
import type { AgentEventId } from "@/shared/api/agents";

interface TurnSeekOptions {
  /** Loaded transcript events. */
  events: ReadonlyArray<{ id: AgentEventId }>;
  hasPreviousPage: boolean;
  isFetchingPreviousPage: boolean;
  fetchPreviousPage: () => unknown;
  /** Scrolls to the turn's row; false when the row is not in the DOM. */
  scrollToTurn: (eventId: AgentEventId) => boolean;
}

/**
 * Jump to a turn whether or not it is loaded: scroll now when its row exists,
 * otherwise page back until the event loads, scroll, and forget the target.
 * The target lives here rather than in the URL, so it never keeps holding the
 * transcript's selection after it lands.
 */
export function useTurnSeek({
  events,
  hasPreviousPage,
  isFetchingPreviousPage,
  fetchPreviousPage,
  scrollToTurn,
}: TurnSeekOptions): { seek: (eventId: AgentEventId) => void; seeking: AgentEventId | null } {
  const [target, setTarget] = useState<AgentEventId | null>(null);
  const loaded = useMemo(
    () => target != null && events.some((event) => String(event.id) === String(target)),
    [target, events],
  );

  useEffect(() => {
    if (target == null) return;
    if (loaded) {
      const frame = window.requestAnimationFrame(() => {
        scrollToTurn(target);
        setTarget(null);
      });
      return () => window.cancelAnimationFrame(frame);
    }
    if (isFetchingPreviousPage) return;
    if (hasPreviousPage) {
      void fetchPreviousPage();
      return;
    }
    // The whole session is loaded and the event is not in it (a rewind).
    setTarget(null);
  }, [target, loaded, isFetchingPreviousPage, hasPreviousPage, fetchPreviousPage, scrollToTurn]);

  const seek = useCallback(
    (eventId: AgentEventId) => {
      if (!scrollToTurn(eventId)) setTarget(eventId);
    },
    [scrollToTurn],
  );
  return { seek, seeking: target };
}
