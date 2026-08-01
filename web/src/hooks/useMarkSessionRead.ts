/**
 * Read-on-open acknowledgement for Console results
 * (control-plane/docs/specs/console-unread-acknowledgement.md).
 *
 * Fires POST /sessions/{id}/read with the result timestamp this client
 * actually rendered — acknowledgement is bounded to what was seen, and only
 * while the tab is visible: a restored background tab must not clear unread
 * it never showed. Re-fires when a turn settles while the workspace stays
 * open (last_result_at moves via the workspace stream). Shared viewers never
 * acknowledge.
 */

import { useEffect } from "react";

import { markSessionRead } from "../services/api/agents";
import { type SessionStateFacts } from "../services/api/agents";

export function useMarkSessionRead({
  sessionId,
  sessionState,
  disabled = false,
}: {
  sessionId: string | null;
  sessionState: SessionStateFacts | null | undefined;
  disabled?: boolean;
}): void {
  const unread = sessionState?.unread === true;
  const readThrough = sessionState?.last_result_at ?? null;

  useEffect(() => {
    if (disabled || !sessionId || !unread || !readThrough) return;
    const fire = (): boolean => {
      if (document.visibilityState !== "visible") return false;
      void markSessionRead(sessionId, readThrough).catch(() => {});
      return true;
    };
    if (fire()) return;
    const onVisible = () => {
      if (fire()) document.removeEventListener("visibilitychange", onVisible);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [disabled, sessionId, unread, readThrough]);
}
