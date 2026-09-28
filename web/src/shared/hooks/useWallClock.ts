import { useEffect, useState } from "react";
import { useDocumentVisible } from "./useDocumentVisible";

/**
 * A clock that re-renders its consumer on a fixed cadence, aligned to the
 * interval boundary and paused while the tab is hidden.
 *
 * Pick the coarsest interval the label actually needs. This was a
 * second-resolution clock serving a running H:MM:SS counter in the session
 * dock, which re-rendered the whole detail page once a second to animate a
 * number nobody was reading.
 */
export function useWallClock(enabled: boolean, intervalMs = 60_000): number {
  const documentVisible = useDocumentVisible();
  const active = enabled && documentVisible;
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (!active) {
      return;
    }

    let intervalId: number | null = null;
    const scheduleRepeatingUpdates = () => {
      setNowMs(Date.now());
      intervalId = window.setInterval(() => {
        setNowMs(Date.now());
      }, intervalMs);
    };

    setNowMs(Date.now());
    const delayUntilNextTick = Math.max(1, intervalMs - (Date.now() % intervalMs));
    const timeoutId = window.setTimeout(scheduleRepeatingUpdates, delayUntilNextTick);

    return () => {
      window.clearTimeout(timeoutId);
      if (intervalId !== null) {
        window.clearInterval(intervalId);
      }
    };
  }, [active, intervalMs]);

  return nowMs;
}
