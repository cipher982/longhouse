import { useCallback, useEffect, useRef, useState } from "react";

/**
 * The demo's clock: a looping requestAnimationFrame timeline quantized to
 * 30fps (the recordings' cadence — finer steps would re-render the React
 * tree for identical frames).
 *
 * Pauses when the demo scrolls offscreen or the tab hides; reduced-motion
 * users get a frozen poster frame. All animation derives from `tSec`, so
 * pausing and seeking are trivially consistent — there is no CSS-animation
 * state to desync.
 */

const TICK = 1 / 30;

export interface DemoClock {
  tSec: number;
  cycle: number;
  playing: boolean;
  reducedMotion: boolean;
  seek: (tSec: number) => void;
  containerRef: React.RefObject<HTMLDivElement | null>;
}

export function advanceDemoClock(
  positionSec: number,
  cycle: number,
  dtSec: number,
  durationSec: number,
): { positionSec: number; cycle: number } {
  const next = Math.max(0, positionSec) + Math.max(0, dtSec);
  const wraps = Math.floor(next / durationSec);
  return {
    positionSec: next % durationSec,
    cycle: cycle + wraps,
  };
}

function usePrefersReducedMotion(): boolean {
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(
    () =>
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches,
  );

  useEffect(() => {
    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setPrefersReducedMotion(mediaQuery.matches);
    update();
    mediaQuery.addEventListener("change", update);
    return () => mediaQuery.removeEventListener("change", update);
  }, []);

  return prefersReducedMotion;
}

export function useDemoClock(durationSec: number, posterSec: number): DemoClock {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const reducedMotion = usePrefersReducedMotion();
  const [isInViewport, setIsInViewport] = useState(true);
  const [isDocumentVisible, setIsDocumentVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState !== "hidden",
  );

  const clockRef = useRef(0);
  const cycleRef = useRef(0);
  const [tSec, setTSec] = useState(0);
  const [cycle, setCycle] = useState(0);
  const playing = !reducedMotion && isInViewport && isDocumentVisible;

  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(([entry]) => {
      setIsInViewport(entry?.isIntersecting ?? false);
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const update = () => setIsDocumentVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  useEffect(() => {
    if (reducedMotion) {
      clockRef.current = posterSec;
      cycleRef.current = 0;
      setTSec(posterSec);
      setCycle(0);
    }
  }, [reducedMotion, posterSec]);

  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    let last: number | null = null;
    const step = (now: number) => {
      if (last !== null) {
        // Cap dt so a background-throttled tab doesn't skip beats on return.
        const dt = Math.min((now - last) / 1000, 0.25);
        const next = advanceDemoClock(
          clockRef.current,
          cycleRef.current,
          dt,
          durationSec,
        );
        clockRef.current = next.positionSec;
        if (next.cycle !== cycleRef.current) {
          cycleRef.current = next.cycle;
          setCycle(next.cycle);
        }
        const quantized = Math.floor(clockRef.current / TICK) * TICK;
        setTSec((prev) => (prev === quantized ? prev : quantized));
      }
      last = now;
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [playing, durationSec]);

  const seek = useCallback(
    (target: number) => {
      const wrapped = ((target % durationSec) + durationSec) % durationSec;
      clockRef.current = wrapped;
      setTSec(Math.floor(wrapped / TICK) * TICK);
    },
    [durationSec],
  );

  return { tSec, cycle, playing, reducedMotion, seek, containerRef };
}
