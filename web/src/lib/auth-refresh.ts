/**
 * Single-flight 401 interceptor with automatic token refresh.
 *
 * When a request gets a 401, we attempt ONE silent refresh via
 * POST /api/auth/refresh (the refresh cookie is sent automatically).
 * If it succeeds, the original request is retried with the new AT cookie.
 * If it fails, we redirect to /login.
 *
 * Only one refresh can be in-flight at a time (mutex). Concurrent 401s
 * queue behind the same refresh promise to avoid rotation races.
 */

import { config } from "./config";
import { buildLoginUrl } from "./loginRedirect";

// ---------------------------------------------------------------------------
// Single-flight mutex
// ---------------------------------------------------------------------------

let refreshPromise: Promise<boolean> | null = null;

async function doRefresh(): Promise<boolean> {
  try {
    const res = await fetch(`${config.apiBaseUrl}/auth/refresh`, {
      method: "POST",
      credentials: "include",
    });
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * Attempt a single-flight token refresh. Returns true if a new AT was issued.
 */
export async function refreshAccessToken(): Promise<boolean> {
  if (refreshPromise) {
    return refreshPromise;
  }
  refreshPromise = doRefresh().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

// ---------------------------------------------------------------------------
// Intercepted fetch
// ---------------------------------------------------------------------------

/**
 * Drop-in replacement for `fetch()` that transparently retries on 401
 * after a silent token refresh.
 *
 * Use this for any browser-authenticated API call that should survive
 * access-token expiry.
 */
export async function fetchWithRefresh(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const response = await fetch(input, init);

  if (response.status !== 401) {
    return response;
  }

  // Don't retry auth endpoints — /auth/refresh would loop, /auth/logout is a
  // deliberate sign-out. "/auth/login" no longer exists but is kept as a guard
  // in case a server-side redirect ever produces that path.
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  if (
    url.includes("/auth/refresh") ||
    url.includes("/auth/logout") ||
    url.includes("/auth/login")
  ) {
    return response;
  }

  const refreshed = await refreshAccessToken();
  if (!refreshed) {
    // Refresh failed — session is dead. Redirect to /login preserving the current page.
    const returnTo = window.location.pathname + window.location.search + window.location.hash;
    window.location.replace(buildLoginUrl(returnTo));
    return response;
  }

  // Retry the original request with the new cookie.
  return fetch(input, init);
}
