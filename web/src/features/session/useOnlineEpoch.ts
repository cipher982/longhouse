import { useEffect, useState } from "react";

/** Returns a value that increments on every `online` event.
 *
 * Useful as a React effect dependency to force re-subscription of
 * long-lived connections (EventSource, WebSocket) after a network flake,
 * since neither browser API re-opens automatically for every recoverable
 * drop. Returns 0 in SSR.
 */
export function useOnlineEpoch(): number {
  const [epoch, setEpoch] = useState(0);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const bump = () => setEpoch((n) => n + 1);
    window.addEventListener("online", bump);
    return () => window.removeEventListener("online", bump);
  }, []);

  return epoch;
}
