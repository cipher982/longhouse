/**
 * Footer-level API health derived from tracked React Query state.
 *
 * Queries that should surface degraded API status opt in with `meta.apiHealth`.
 * That keeps the footer tied to the data layer instead of mirroring page-local
 * query errors into a second store via useEffect.
 *
 * Only two failures say anything about Longhouse as a whole: no answer at all
 * (network down, timeout, or an edge proxy page because the server is gone)
 * and an answer that is a server error. A 4xx is about one request (auth has
 * its own sign-in path), so it never flips the global pill.
 */
import { useSyncExternalStore } from "react";
import { useQueryClient, type Query, type QueryClient } from "@tanstack/react-query";
import { ApiError } from "@/shared/api/base";

export type ApiHealthProblem = {
  kind: "unreachable" | "server_error";
  /** What the pill says. */
  label: string;
  /** Tooltip / screen-reader detail: status, code and message when known. */
  detail: string;
  error: Error;
};

function queryAffectsApiHealth(query: Query): boolean {
  return Boolean((query.meta as { apiHealth?: boolean } | undefined)?.apiHealth);
}

function toError(value: unknown): Error | null {
  if (!value) return null;
  return value instanceof Error ? value : new Error(String(value));
}

function errorCode(body: unknown): string | null {
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const detail = (body as { detail?: unknown }).detail;
  if (detail && typeof detail === "object" && "code" in detail && typeof detail.code === "string") {
    return detail.code;
  }
  return null;
}

export function classifyApiHealthError(error: Error): ApiHealthProblem | null {
  if (!(error instanceof ApiError)) {
    return {
      kind: "unreachable",
      label: "Can't reach Longhouse",
      detail: error.message,
      error,
    };
  }
  if (error.status < 500) return null;
  // Longhouse answers errors as JSON. A 5xx without a JSON body is an edge
  // proxy (Cloudflare 502/504/52x) saying the server never answered.
  const answeredByLonghouse = Boolean(error.body) && typeof error.body === "object";
  if (!answeredByLonghouse) {
    return {
      kind: "unreachable",
      label: "Can't reach Longhouse",
      detail: `HTTP ${error.status} from the network before Longhouse answered.`,
      error,
    };
  }
  const code = errorCode(error.body);
  return {
    kind: "server_error",
    label: "Longhouse had a problem",
    detail: [`HTTP ${error.status}`, code, error.message].filter(Boolean).join(" · "),
    error,
  };
}

function getSnapshot(queryClient: QueryClient): ApiHealthProblem | null {
  let latest: { problem: ApiHealthProblem; updatedAt: number } | null = null;

  for (const query of queryClient.getQueryCache().getAll()) {
    if (!queryAffectsApiHealth(query)) continue;
    const error = toError(query.state.error);
    if (!error) continue;
    const problem = classifyApiHealthError(error);
    if (!problem) continue;
    const updatedAt = query.state.errorUpdatedAt || 0;
    if (!latest || updatedAt >= latest.updatedAt) {
      latest = { problem, updatedAt };
    }
  }

  return latest?.problem ?? null;
}

// useSyncExternalStore compares snapshots by identity, so hand back the same
// object while the underlying error is unchanged.
function stableSnapshot(queryClient: QueryClient, cache: { last: ApiHealthProblem | null }): ApiHealthProblem | null {
  const next = getSnapshot(queryClient);
  if (next?.error === cache.last?.error) return cache.last;
  cache.last = next;
  return next;
}

function subscribe(queryClient: QueryClient, onStoreChange: () => void): () => void {
  return queryClient.getQueryCache().subscribe((event) => {
    if (!event?.query || !queryAffectsApiHealth(event.query)) return;
    onStoreChange();
  });
}

const snapshotCaches = new WeakMap<QueryClient, { last: ApiHealthProblem | null }>();

export function useApiHealth(): ApiHealthProblem | null {
  const queryClient = useQueryClient();
  let cache = snapshotCaches.get(queryClient);
  if (!cache) {
    cache = { last: null };
    snapshotCaches.set(queryClient, cache);
  }
  const snapshotCache = cache;
  return useSyncExternalStore(
    (onStoreChange) => subscribe(queryClient, onStoreChange),
    () => stableSnapshot(queryClient, snapshotCache),
    () => stableSnapshot(queryClient, snapshotCache),
  );
}
