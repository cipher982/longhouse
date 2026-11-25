import { useEffect, useRef, useState } from "react";

/** Minimum hidden duration before resuming triggers a fresh revalidation. */
export const RESUME_REVALIDATION_GAP_MS = 5_000;
/** Safety cap so an offline tab never stays suppressed forever. */
const RESUME_REVALIDATION_TIMEOUT_MS = 4_000;

/**
 * Detect returning to a tab after being hidden for at least `gapMs`, trigger
 * an immediate `refetch()` (bypassing React Query's staleTime), and return
 * `true` while that revalidation is in flight so stale working indicators can
 * wait for fresh truth instead of flashing pre-background state.
 */
export function useResumeRevalidation(
  documentVisible: boolean,
  refetch: () => Promise<unknown>,
  options: { enabled?: boolean; gapMs?: number } = {},
): boolean {
  const { enabled = true, gapMs = RESUME_REVALIDATION_GAP_MS } = options;
  const [revalidating, setRevalidating] = useState(false);
  const hiddenAtRef = useRef<number | null>(null);
  const wasVisibleRef = useRef(documentVisible);
  const refetchRef = useRef(refetch);
  refetchRef.current = refetch;

  useEffect(() => {
    if (!enabled) {
      hiddenAtRef.current = null;
      wasVisibleRef.current = documentVisible;
      setRevalidating(false);
      return;
    }

    const wasVisible = wasVisibleRef.current;
    wasVisibleRef.current = documentVisible;

    if (!documentVisible) {
      if (wasVisible || hiddenAtRef.current === null) {
        hiddenAtRef.current = Date.now();
      }
      setRevalidating(false);
      return;
    }

    if (wasVisible || hiddenAtRef.current === null) {
      return;
    }

    const gap = Date.now() - hiddenAtRef.current;
    hiddenAtRef.current = null;
    if (gap < gapMs) {
      return;
    }

    let cancelled = false;
    setRevalidating(true);
    const timeoutId = window.setTimeout(() => {
      if (!cancelled) setRevalidating(false);
    }, RESUME_REVALIDATION_TIMEOUT_MS);

    void Promise.resolve(refetchRef.current())
      .catch(() => {})
      .finally(() => {
        window.clearTimeout(timeoutId);
        if (!cancelled) setRevalidating(false);
      });

    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
    };
  }, [documentVisible, enabled, gapMs]);

  return revalidating;
}
