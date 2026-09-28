/**
 * Footer-level API health derived from tracked React Query state.
 *
 * Queries that should surface degraded API status opt in with `meta.apiHealth`.
 * That keeps the footer tied to the data layer instead of mirroring page-local
 * query errors into a second store via useEffect.
 */
import { useSyncExternalStore } from "react";
import { useQueryClient, type Query, type QueryClient } from "@tanstack/react-query";

function queryAffectsApiHealth(query: Query): boolean {
  return Boolean((query.meta as { apiHealth?: boolean } | undefined)?.apiHealth);
}

function toError(value: unknown): Error | null {
  if (!value) return null;
  return value instanceof Error ? value : new Error(String(value));
}

function getSnapshot(queryClient: QueryClient): Error | null {
  let latest: { error: Error; updatedAt: number } | null = null;

  for (const query of queryClient.getQueryCache().getAll()) {
    if (!queryAffectsApiHealth(query)) continue;
    const error = toError(query.state.error);
    if (!error) continue;
    const updatedAt = query.state.errorUpdatedAt || 0;
    if (!latest || updatedAt >= latest.updatedAt) {
      latest = { error, updatedAt };
    }
  }

  return latest?.error ?? null;
}

function subscribe(queryClient: QueryClient, onStoreChange: () => void): () => void {
  return queryClient.getQueryCache().subscribe((event) => {
    if (!event?.query || !queryAffectsApiHealth(event.query)) return;
    onStoreChange();
  });
}

export function useApiHealth(): Error | null {
  const queryClient = useQueryClient();
  return useSyncExternalStore(
    (onStoreChange) => subscribe(queryClient, onStoreChange),
    () => getSnapshot(queryClient),
    () => getSnapshot(queryClient),
  );
}
