import { useEffect } from "react";

/**
 * Route chunks the app preloads before they are needed. The lazy route and
 * the preload call the same import(), so the browser fetches the chunk once
 * and React finds it already resolved.
 */
export const loadSessionDetailPage = () => import("@/features/session/SessionDetailPage");
export const loadMachinesPage = () => import("@/features/machines/MachinesPage");

let sessionDetailRequested = false;

/** Start downloading the session page's code; safe to call any number of times. */
export function preloadSessionDetailPage(): void {
  if (sessionDetailRequested) return;
  sessionDetailRequested = true;
  void loadSessionDetailPage().catch(() => {
    // A failed preload is retried by the route itself.
    sessionDetailRequested = false;
  });
}

let machinesPageRequested = false;

/** Start downloading the Machines route code; safe to call repeatedly. */
export function preloadMachinesPage(): void {
  if (machinesPageRequested) return;
  machinesPageRequested = true;
  void loadMachinesPage().catch(() => {
    // A failed preload is retried by the route itself.
    machinesPageRequested = false;
  });
}

/**
 * Preload the session page's code once the current page has gone idle, so the
 * first click into a session does not wait on a download.
 */
export function useIdlePreloadSessionDetailPage(): void {
  useEffect(() => {
    if (typeof window === "undefined") return;
    // Safari has no requestIdleCallback; a short timer stands in for it.
    if (typeof window.requestIdleCallback === "function") {
      const handle = window.requestIdleCallback(preloadSessionDetailPage, { timeout: 2_000 });
      return () => window.cancelIdleCallback(handle);
    }
    const handle = globalThis.setTimeout(preloadSessionDetailPage, 400);
    return () => globalThis.clearTimeout(handle);
  }, []);
}
