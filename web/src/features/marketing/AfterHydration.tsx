import { Suspense, useSyncExternalStore, type ReactNode } from "react";

const subscribe = () => () => {};

/**
 * Mounts a lazily loaded section (a terminal demo, the recorded scene) once the
 * browser is running the page. The prerendered HTML and the hydration pass show
 * `fallback`, the same placeholder shown while the section's chunk loads: the
 * demos are animated client state with their CSS in the lazy chunk, so putting
 * them in the static page would only add unstyled demo chrome to what crawlers
 * read. A client-only render (opening the landing page from inside the app)
 * skips straight to the section.
 */
export function AfterHydration({ fallback, children }: { fallback: ReactNode; children: ReactNode }) {
  const hydrated = useSyncExternalStore(
    subscribe,
    () => true,
    () => false,
  );
  return <Suspense fallback={fallback}>{hydrated ? children : fallback}</Suspense>;
}
