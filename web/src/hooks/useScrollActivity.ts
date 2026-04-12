import { useCallback, useEffect, useRef } from "react";

const DEFAULT_SUPPRESSION_MS = 250;

interface UseScrollActivityOptions {
  /** CSS class added to the scroll element while scrolling. */
  scrollClass?: string;
  /** CSS class added to #react-root while scrolling (for shell-level suppression). */
  rootClass?: string;
  /** How long after the last scroll event to keep the active classes. Default 250ms. */
  suppressionMs?: number;
  /** Optional callback fired on each scroll activity event. */
  onActivity?: () => void;
}

/**
 * Attaches scroll/wheel/touchmove listeners to a target element and toggles
 * CSS classes during active scrolling. Used to suppress hover transitions and
 * decorative animations that cause raster churn when content moves under a
 * stationary cursor.
 *
 * Returns `getLastScrollAt()` so callers can gate hover-intent logic.
 */
export function useScrollActivity(
  getElement: () => HTMLElement | null,
  {
    scrollClass = "scrolling-active",
    rootClass,
    suppressionMs = DEFAULT_SUPPRESSION_MS,
    onActivity,
  }: UseScrollActivityOptions = {}
) {
  const timeoutRef = useRef<number | null>(null);
  const lastScrollAtRef = useRef(0);
  const onActivityRef = useRef(onActivity);

  useEffect(() => {
    onActivityRef.current = onActivity;
  }, [onActivity]);

  const getLastScrollAt = useCallback(() => lastScrollAtRef.current, []);

  useEffect(() => {
    const el = getElement();
    if (!el) return;

    const appRoot = rootClass ? document.getElementById("react-root") : null;

    const clearTimer = () => {
      if (timeoutRef.current != null) {
        window.clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
      }
    };

    const markScrolling = () => {
      lastScrollAtRef.current = performance.now();
      el.classList.add(scrollClass);
      if (rootClass) appRoot?.classList.add(rootClass);
      onActivityRef.current?.();
      clearTimer();
      timeoutRef.current = window.setTimeout(() => {
        timeoutRef.current = null;
        el.classList.remove(scrollClass);
        if (rootClass) appRoot?.classList.remove(rootClass);
      }, suppressionMs);
    };

    el.addEventListener("wheel", markScrolling, { passive: true });
    el.addEventListener("touchmove", markScrolling, { passive: true });
    el.addEventListener("scroll", markScrolling, { passive: true });

    return () => {
      clearTimer();
      el.classList.remove(scrollClass);
      if (rootClass) appRoot?.classList.remove(rootClass);
      el.removeEventListener("wheel", markScrolling);
      el.removeEventListener("touchmove", markScrolling);
      el.removeEventListener("scroll", markScrolling);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrollClass, rootClass, suppressionMs]);

  return { getLastScrollAt };
}
