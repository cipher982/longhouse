import { useCallback, useEffect, useRef, useState } from "react";

const TICK = 1 / 30;

interface ReplayClock {
  tSec: number;
  reducedMotion: boolean;
  containerRef: React.RefObject<HTMLDivElement | null>;
  start: () => void;
}

function usePrefersReducedMotion(): boolean {
  const [reducedMotion, setReducedMotion] = useState(
    () =>
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches,
  );

  useEffect(() => {
    if (!window.matchMedia) return;
    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(mediaQuery.matches);
    update();
    mediaQuery.addEventListener?.("change", update);
    return () => mediaQuery.removeEventListener?.("change", update);
  }, []);

  return reducedMotion;
}

/** A finite, offscreen-pausing rAF timeline for an interaction replay. */
export function useReplayClock(durationSec: number, resetKey: string): ReplayClock {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const reducedMotion = usePrefersReducedMotion();
  const [isInViewport, setIsInViewport] = useState(true);
  const [isDocumentVisible, setIsDocumentVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState !== "hidden",
  );
  const clockRef = useRef(0);
  const [tSec, setTSec] = useState(0);
  const [started, setStarted] = useState(false);

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
    clockRef.current = 0;
    setTSec(0);
    setStarted(false);
  }, [durationSec, resetKey]);

  useEffect(() => {
    if (!started || reducedMotion || !isInViewport || !isDocumentVisible) return;

    let raf = 0;
    let last: number | null = null;
    const step = (now: number) => {
      if (last !== null) {
        const dt = Math.min((now - last) / 1000, 0.25);
        const next = Math.min(clockRef.current + dt, durationSec);
        clockRef.current = next;
        const quantized = Math.floor(next / TICK) * TICK;
        setTSec((prev) => (prev === quantized ? prev : quantized));
        if (next >= durationSec) {
          setTSec(durationSec);
          setStarted(false);
          return;
        }
      }
      last = now;
      raf = requestAnimationFrame(step);
    };

    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [durationSec, isDocumentVisible, isInViewport, reducedMotion, started]);

  const start = useCallback(() => {
    clockRef.current = reducedMotion ? durationSec : 0;
    setTSec(reducedMotion ? durationSec : 0);
    setStarted(true);
  }, [durationSec, reducedMotion]);

  return { tSec, reducedMotion, containerRef, start };
}
