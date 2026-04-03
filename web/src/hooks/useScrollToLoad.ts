import { useEffect, useRef } from "react";

/**
 * Calls `onLoad` when the sentinel element scrolls into view inside `rootRef`.
 *
 * Two triggers:
 * 1. IntersectionObserver — fires when the sentinel enters the scroll container.
 * 2. Post-load check — when a load completes (loading: true→false), if the
 *    sentinel is still visible, fires again immediately so the user doesn't
 *    have to scroll down and back up to "reset" the observer.
 *
 * All callbacks/state are kept in refs so the observer is only re-created
 * when `enabled` actually changes (no more pages → disconnect).
 */
export function useScrollToLoad(options: {
  sentinelRef: React.RefObject<HTMLDivElement | null>;
  /** Scroll container — pass the overflow div so intersection is relative to
   *  it, not the viewport. Required when the sentinel lives inside a
   *  scrollable div. */
  rootRef?: React.RefObject<HTMLDivElement | null>;
  enabled: boolean;
  loading: boolean;
  onLoad: () => void;
}) {
  const { sentinelRef, rootRef, enabled, loading, onLoad } = options;

  const loadingRef = useRef(loading);
  const onLoadRef = useRef(onLoad);
  const isIntersectingRef = useRef(false);

  useEffect(() => { loadingRef.current = loading; }, [loading]);
  useEffect(() => { onLoadRef.current = onLoad; }, [onLoad]);

  // When a fetch completes and the sentinel is still visible, fire immediately
  // instead of waiting for another intersection event (which won't come because
  // the state didn't change — the sentinel was already intersecting).
  const prevLoadingRef = useRef(loading);
  useEffect(() => {
    const wasLoading = prevLoadingRef.current;
    prevLoadingRef.current = loading;
    if (wasLoading && !loading && isIntersectingRef.current && enabled) {
      onLoadRef.current();
    }
  }, [loading, enabled]);

  useEffect(() => {
    if (!enabled) return;
    const sentinel = sentinelRef.current;
    if (!sentinel) return;

    let skipFirst = true;

    const observer = new IntersectionObserver(
      ([entry]) => {
        isIntersectingRef.current = entry.isIntersecting;
        if (skipFirst) {
          skipFirst = false;
          return;
        }
        if (entry.isIntersecting && !loadingRef.current) {
          onLoadRef.current();
        }
      },
      { root: rootRef?.current ?? null, threshold: 0 },
    );

    observer.observe(sentinel);
    return () => observer.disconnect();
  // loading/onLoad intentionally excluded — tracked via refs.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, sentinelRef]);
}
