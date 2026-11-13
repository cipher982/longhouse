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
  const autoFillCountRef = useRef(0);
  useEffect(() => { loadingRef.current = loading; }, [loading]);
  useEffect(() => { onLoadRef.current = onLoad; }, [onLoad]);

  useEffect(() => {
    if (!enabled) {
      autoFillCountRef.current = 0;
      return;
    }
    const sentinel = sentinelRef.current;
    if (!sentinel) return;

    let skipFirst = true;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (skipFirst) {
          skipFirst = false;
          if (!entry.isIntersecting) return;
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

  // If the loaded tail fits inside the viewport without overflowing (e.g. a
  // page of tool calls collapsed into one row), there is no scrollbar and
  // IntersectionObserver will not fire again after prepending. Auto-load up to
  // a small bounded number of pages until content overflows.
  useEffect(() => {
    if (!enabled || loading) return;
    const root = rootRef?.current;
    if (!root || root.clientHeight <= 0) return;
    if (root.scrollHeight <= root.clientHeight && autoFillCountRef.current < 5) {
      autoFillCountRef.current += 1;
      onLoadRef.current();
    }
  }, [enabled, loading, rootRef]);
}
