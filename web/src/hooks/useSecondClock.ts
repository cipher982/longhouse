import { useEffect, useState } from "react";
import { useDocumentVisible } from "./useDocumentVisible";

export function useSecondClock(enabled: boolean): number {
  const documentVisible = useDocumentVisible();
  const active = enabled && documentVisible;
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (!enabled) return;
    setNowMs(Date.now());
  }, [enabled]);

  useEffect(() => {
    if (!active) {
      return;
    }

    let intervalId: number | null = null;
    const scheduleRepeatingUpdates = () => {
      setNowMs(Date.now());
      intervalId = window.setInterval(() => {
        setNowMs(Date.now());
      }, 1000);
    };

    setNowMs(Date.now());
    const delayUntilNextSecond = Math.max(1, 1000 - (Date.now() % 1000));
    const timeoutId = window.setTimeout(scheduleRepeatingUpdates, delayUntilNextSecond);

    return () => {
      window.clearTimeout(timeoutId);
      if (intervalId !== null) {
        window.clearInterval(intervalId);
      }
    };
  }, [active]);

  return nowMs;
}
