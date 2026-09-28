/**
 * Default React Query retry policy.
 *
 * Keep React Query's stock behaviour (three retries with backoff) for
 * transient failures -- network errors, 5xx, 408 and 429 -- but fail fast on
 * other 4xx responses. A 404 for a missing session or a 403 will not change
 * on retry; retrying only delayed the error state by ~7s of backoff.
 */
const MAX_RETRIES = 3;
const TRANSIENT_CLIENT_STATUSES = new Set([408, 429]);

function httpStatus(error: unknown): number | null {
  if (error && typeof error === "object" && "status" in error) {
    const status = (error as { status: unknown }).status;
    if (typeof status === "number") return status;
  }
  return null;
}

export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (failureCount >= MAX_RETRIES) return false;
  const status = httpStatus(error);
  if (status !== null && status >= 400 && status < 500) {
    return TRANSIENT_CLIENT_STATUSES.has(status);
  }
  return true;
}
