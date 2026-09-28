import { useEffect, useRef } from "react";

/**
 * Fires `onLoad` when the sentinel scrolls into view inside the scroll
 * container (`rootRef`).
 *
 * Pair this with scroll anchoring on the scroll container so that when new
 * content is prepended the user's visual position is preserved (sentinel moves
 * off-screen naturally). Without anchoring you'd need a "scroll down to reset"
 * dance because IntersectionObserver only fires on intersection *changes*.
 *
 * `loading` and `onLoad` are kept in refs so the observer isn't recreated —
 * and the skip-first-fire flag reset — on every React Query re-render.
 */
export function useScrollToLoad(options: {
  sentinelRef: React.RefObject<HTMLDivElement | null>;
  /** Scroll container — intersection is checked relative to this element
   *  rather than the viewport. Required when the sentinel lives inside an
   *  overflow-y:auto div. */
  rootRef?: React.RefObject<HTMLDivElement | null>;
  enabled: boolean;
  loading: boolean;
  onLoad: () => void;
}) {
  const { sentinelRef, rootRef, enabled, loading, onLoad } = options;

  const loadingRef = useRef(loading);
  const onLoadRef = useRef(onLoad);
  useEffect(() => { loadingRef.current = loading; }, [loading]);
  useEffect(() => { onLoadRef.current = onLoad; }, [onLoad]);

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
      { root: rootRef?.current ?? null, threshold: 0 },
    );

    observer.observe(sentinel);
    return () => observer.disconnect();
  // loading/onLoad intentionally excluded — tracked via refs.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, sentinelRef]);
}
