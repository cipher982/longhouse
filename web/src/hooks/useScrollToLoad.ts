import { useEffect, useRef } from "react";

/**
 * Attaches an IntersectionObserver to a sentinel element and calls `onLoad`
 * when the sentinel scrolls into view — i.e. the user has scrolled to the top.
 *
 * The first intersection notification after attachment is skipped because it
 * fires synchronously on mount before the timeline has had a chance to
 * auto-scroll to the bottom. Subsequent intersections are real "user scrolled
 * to top" events.
 *
 * `onLoad` and `loading` are tracked via refs so the observer is not
 * recreated (and the skip-flag reset) on each React Query re-render that
 * follows a page load. The observer is only created / destroyed when
 * `enabled` changes (i.e., when there are no more pages to load).
 */
export function useScrollToLoad(options: {
  sentinelRef: React.RefObject<HTMLDivElement | null>;
  enabled: boolean;
  loading: boolean;
  onLoad: () => void;
}) {
  const { sentinelRef, enabled, loading, onLoad } = options;

  const loadingRef = useRef(loading);
  useEffect(() => {
    loadingRef.current = loading;
  }, [loading]);

  const onLoadRef = useRef(onLoad);
  useEffect(() => {
    onLoadRef.current = onLoad;
  }, [onLoad]);

  useEffect(() => {
    if (!enabled) return;
    const sentinel = sentinelRef.current;
    if (!sentinel) return;

    let skipFirst = true;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (skipFirst) {
          skipFirst = false;
          return;
        }
        if (entry.isIntersecting && !loadingRef.current) {
          onLoadRef.current();
        }
      },
      { threshold: 0 },
    );

    observer.observe(sentinel);
    return () => observer.disconnect();
  // Only re-create the observer when `enabled` changes. `loading` and
  // `onLoad` are intentionally tracked via refs.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, sentinelRef]);
}
