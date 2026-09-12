/**
 * Single-flight 401 interceptor with automatic token refresh.
 *
 * - one promise per tab;
 * - the Web Locks API when available; server-side replay handling remains
 *   authoritative when browser coordination is unavailable.
 *
 * Logout also has a short-lived cross-tab barrier. A refresh that starts after
 * logout begins must not install a new cookie after the logout response.
 */

import { config } from "./config";
import { replaceWithLoginUrl } from "./loginRedirect";
import { requestNativeAuth } from "./nativeAuthBridge";

const AUTH_CHANNEL_NAME = "longhouse-auth-events";
const LOGOUT_BARRIER_KEY = "longhouse:logout-barrier";
const REFRESH_LOCK_NAME = "longhouse-auth-refresh";
const LOGOUT_BARRIER_TTL_MS = 30_000;
const AUTH_REQUEST_TIMEOUT_MS = 15_000;

const LOGIN_ATTEMPT_KEY = "longhouse:login-attempt";
const LOGGED_OUT_SESSION_KEY = "longhouse:logged-out";
const LOGOUT_GENERATION_KEY = "longhouse:logout-generation";
let loginAttemptGeneration: string | null = null;
let logoutGeneration = "0";

function readLogoutGeneration(): string {
  if (typeof window === "undefined") return logoutGeneration;
  let storageReadFailed = false;
  let found = false;
  for (const storage of ["localStorage", "sessionStorage"] as const) {
    try {
      const stored = window[storage].getItem(LOGOUT_GENERATION_KEY);
      if (stored && /^\d+$/.test(stored)) {
        found = true;
        logoutGeneration = String(Math.max(Number(stored), Number(logoutGeneration)));
      }
    } catch {
      storageReadFailed = true;
      // Storage can be disabled independently; keep checking the other
      // durable/in-tab fence and the in-memory generation.
    }
  }
  if (!found && !storageReadFailed) logoutGeneration = "0";
  return logoutGeneration;
}

function advanceLogoutGeneration(): void {
  const current = Number(readLogoutGeneration());
  logoutGeneration = String(Math.max(Date.now(), current + 1));
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LOGOUT_GENERATION_KEY, logoutGeneration);
  } catch {
    // The current tab remains protected by the in-memory generation.
  }
  try {
    window.sessionStorage.setItem(LOGOUT_GENERATION_KEY, logoutGeneration);
  } catch {
    // Cross-tab storage is best effort; the cookie marker still fences
    // server-issued handoff completion in this browser.
  }
  // The tenant cannot write sessionStorage for a handoff initiated from the
  // control-plane dashboard. Keep the current client generation in the
  // host-only marker cookie so that a server-issued login-ready signal can
  // still be associated with the latest explicit logout fence.
  setLoginAttemptCookie(logoutGeneration);
}

function loginAttemptCookieName(): string {
  return window.location.protocol === "https:" ? "__Host-lh_login_attempt" : "lh_login_attempt";
}

function setLoginAttemptCookie(generation: string): void {
  if (typeof window === "undefined") return;
  const secure = window.location.protocol === "https:" ? " Secure;" : "";
  document.cookie = `${loginAttemptCookieName()}=${generation}; Max-Age=${30 * 24 * 60 * 60}; Path=/; SameSite=Lax;${secure}`;
}

export function markLoginAttempt(): void {
  const generation = readLogoutGeneration();
  // The first hosted handoff predates any durable logout generation and the
  // server uses "1" as that pre-generation marker.
  loginAttemptGeneration = generation === "0" ? "1" : generation;
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(LOGIN_ATTEMPT_KEY, loginAttemptGeneration);
  } catch {
    // The in-memory marker still covers this tab.
  }
  setLoginAttemptCookie(loginAttemptGeneration);
}


export function clearLoginAttempt(): void {
  loginAttemptGeneration = null;
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(LOGIN_ATTEMPT_KEY);
  } catch {
    // Nothing else is required; the per-tab marker is already cleared.
  }
}
function hasLocalLogoutIntent(): boolean {
  if (typeof window === "undefined") return false;
  for (const storage of [window.localStorage, window.sessionStorage]) {
    try {
      if (storage.getItem(LOGGED_OUT_SESSION_KEY) === "1") return true;
    } catch {
      // A disabled storage area cannot override the other fence.
    }
  }
  return false;
}

function readCookieValue(name: string): string | null {
  if (typeof document === "undefined") return null;
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) return trimmed.slice(prefix.length);
  }
  return null;
}

function isCurrentLoginGeneration(marker: string | null): boolean {
  if (!marker) return false;
  const current = readLogoutGeneration();
  // "1" is the pre-generation server marker. It remains valid only before
  // the first durable logout generation exists.
  return marker === current || (marker === "1" && current === "0");
}

function readLoginAttemptGeneration(): string | null {
  let marker = loginAttemptGeneration;
  if (typeof window !== "undefined") {
    try {
      const stored = window.sessionStorage.getItem(LOGIN_ATTEMPT_KEY);
      if (!marker) marker = stored;
    } catch {
      // The host-only cookie is the cross-origin handoff fallback.
    }
  }
  return marker ?? readCookieValue(loginAttemptCookieName());
}

export function currentLoginAttemptGeneration(): string | null {
  const marker = readLoginAttemptGeneration();
  return marker && isCurrentLoginGeneration(marker) ? marker : null;
}

export function consumeLoginAttempt(expectedMarker?: string): boolean {
  const marker = currentLoginAttemptGeneration();
  if (!marker || (expectedMarker !== undefined && marker !== expectedMarker)) {
    return false;
  }
  loginAttemptGeneration = null;
  if (typeof window !== "undefined") {
    try {
      window.sessionStorage.removeItem(LOGIN_ATTEMPT_KEY);
    } catch {
      // The host-only cookie remains the cross-origin fallback marker.
    }
  }
  return true;
}

function loginReadyCookieName(): string {
  return window.location.protocol === "https:" ? "__Host-lh_login_ready" : "lh_login_ready";
}

export function loginReadyGeneration(): string | null {
  return readCookieValue(loginReadyCookieName());
}

export function consumeLoginReadySignal(expectedMarker?: string): boolean {
  const marker = loginReadyGeneration();
  if (!marker || (expectedMarker !== undefined && marker !== expectedMarker)) {
    return false;
  }
  const secure = window.location.protocol === "https:" ? " Secure;" : "";
  document.cookie = `${loginReadyCookieName()}=; Max-Age=0; Path=/; SameSite=Lax;${secure}`;
  return true;
}


let refreshPromise: Promise<boolean> | null = null;
let refreshController: AbortController | null = null;
let logoutBarrierActive = false;
let logoutBarrierExpiresAt = 0;
let lifecycleInstalled = false;
let lifecycleChannel: BroadcastChannel | null = null;
export class RefreshUnavailableError extends Error {
  constructor() {
    super("Authentication service temporarily unavailable");
    this.name = "RefreshUnavailableError";
  }
}

function isTransientRefreshStatus(status: number): boolean {
  return status === 408 || status === 409 || status === 425 || status === 429 || status >= 500;
}

function installLifecycleListeners(): void {
  if (typeof window === "undefined" || lifecycleInstalled) return;
  lifecycleInstalled = true;
  const onMessage = (event: MessageEvent<{ type?: string; expiresAt?: number }>) => {
    if (event.data?.type === "logout-start") {
      logoutBarrierActive = true;
      logoutBarrierExpiresAt = Number.isFinite(event.data.expiresAt)
        ? Number(event.data.expiresAt)
        : Date.now() + LOGOUT_BARRIER_TTL_MS;
      cancelRefresh();
    } else if (event.data?.type === "login-ready") {
      logoutBarrierActive = false;
      logoutBarrierExpiresAt = 0;
    }
  };
  if (typeof window.BroadcastChannel === "function") {
    lifecycleChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    lifecycleChannel.onmessage = onMessage;
  } else {
    window.addEventListener("storage", (event) => {
      if (event.key !== LOGOUT_BARRIER_KEY) return;
      if (event.newValue) {
        logoutBarrierActive = true;
        logoutBarrierExpiresAt = Number(event.newValue) || Date.now() + LOGOUT_BARRIER_TTL_MS;
        cancelRefresh();
      } else {
        logoutBarrierActive = false;
        logoutBarrierExpiresAt = 0;
      }
    });
  }
}

function broadcastLifecycle(type: "logout-start" | "login-ready"): void {
  installLifecycleListeners();
  lifecycleChannel?.postMessage({
    type,
    expiresAt: type === "logout-start" ? logoutBarrierExpiresAt : undefined,
  });
}

function readLogoutBarrier(): boolean {
  const now = Date.now();
  if (logoutBarrierActive) {
    if (logoutBarrierExpiresAt > now) return true;
    logoutBarrierActive = false;
    logoutBarrierExpiresAt = 0;
    if (typeof window !== "undefined") {
      try {
        window.localStorage.removeItem(LOGOUT_BARRIER_KEY);
      } catch {
        // The in-memory fence has already expired.
      }
    }
  }
  if (typeof window === "undefined") return false;
  try {
    const expiresAt = Number(window.localStorage.getItem(LOGOUT_BARRIER_KEY));
    if (Number.isFinite(expiresAt) && expiresAt > now) {
      logoutBarrierActive = true;
      logoutBarrierExpiresAt = expiresAt;
      return true;
    }
  } catch {
    // The in-memory state is authoritative when storage is unavailable.
  }
  return false;
}

export function isLogoutBarrierActive(): boolean {
  return readLogoutBarrier();
}

/** Prevent refreshes in every tab while a logout request is in flight. */
export function beginLogoutBarrier(): void {
  advanceLogoutGeneration();
  clearLoginAttempt();
  logoutBarrierActive = true;
  logoutBarrierExpiresAt = Date.now() + LOGOUT_BARRIER_TTL_MS;
  try {
    window.localStorage.setItem(LOGOUT_BARRIER_KEY, String(logoutBarrierExpiresAt));
  } catch {
    // The current tab remains protected by the in-memory expiry.
  }
  cancelRefresh();
  broadcastLifecycle("logout-start");
}

/** Re-enable refresh after a logout request failed and kept the session. */
export function clearLogoutBarrier(): void {
  logoutBarrierActive = false;
  logoutBarrierExpiresAt = 0;
  if (typeof window !== "undefined") {
    try {
      window.localStorage.removeItem(LOGOUT_BARRIER_KEY);
    } catch {
      // Nothing else is required; the in-memory fence is authoritative here.
    }
  }
  broadcastLifecycle("login-ready");
}



async function withRefreshLock<T>(signal: AbortSignal, operation: () => Promise<T>): Promise<T> {
  if (typeof navigator !== "undefined" && navigator.locks) {
    try {
      return await navigator.locks.request(
        REFRESH_LOCK_NAME,
        { mode: "exclusive", signal },
        async () => {
          if (signal.aborted || readLogoutBarrier() || hasLocalLogoutIntent()) {
            throw new RefreshUnavailableError();
          }
          return operation();
        },
      );
    } catch (error) {
      if (signal.aborted || error instanceof DOMException && error.name === "AbortError") {
        throw new RefreshUnavailableError();
      }
      throw error;
    }
  }
  if (signal.aborted || readLogoutBarrier() || hasLocalLogoutIntent()) throw new RefreshUnavailableError();
  // Browser coordination is an optimization. The refresh endpoint owns
  // rotation replay and family revocation, so do not emulate a lock with a
  // non-atomic localStorage lease.
  return operation();
}

async function doRefresh(signal: AbortSignal): Promise<boolean> {
  let res: Response;
  try {
    res = await fetch(`${config.apiBaseUrl}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: { "X-Longhouse-Auth": "1" },
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new RefreshUnavailableError();
    }
    throw new RefreshUnavailableError();
  }

  if (signal.aborted || readLogoutBarrier() || hasLocalLogoutIntent()) {
    throw new RefreshUnavailableError();
  }
  if (isTransientRefreshStatus(res.status)) {
    throw new RefreshUnavailableError();
  }
  return res.ok;
}

/**
 * Attempt a single-flight token refresh. Returns true if a new AT was issued.
 */
export async function refreshAccessToken(): Promise<boolean> {
  installLifecycleListeners();
  if (readLogoutBarrier() || hasLocalLogoutIntent()) throw new RefreshUnavailableError();
  if (refreshPromise) {
    return refreshPromise;
  }
  const controller = new AbortController();
  refreshController = controller;
  const timeout = setTimeout(() => controller.abort(), AUTH_REQUEST_TIMEOUT_MS);
  const promise = withRefreshLock(controller.signal, () => doRefresh(controller.signal)).finally(() => {
    clearTimeout(timeout);
    if (refreshPromise === promise) {
      refreshPromise = null;
      refreshController = null;
    }
  });
  refreshPromise = promise;
  return promise;
}

/** Abort a refresh before a logout request can install new cookies. */
export function cancelRefresh(): void {
  refreshController?.abort();
  refreshController = null;
  refreshPromise = null;
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
  // Keep the original fetch arguments for callers/tests that inspect them, but
  // materialize a replayable Request before the first attempt. This preserves
  // one-shot bodies for the retry without changing the public fetch shape.
  const request = new Request(
    input instanceof Request
      ? input
      : new URL(input instanceof URL ? input.href : input, window.location.origin),
    init,
  );
  if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method) && !request.headers.has("X-Longhouse-Auth")) {
    request.headers.set("X-Longhouse-Auth", "1");
  }
  const response = await fetch(request.clone());

  if (response.status !== 401) {
    return response;
  }

  // Do not retry auth endpoints. Match the pathname, not the raw URL, so a
  // return_to query value containing "/auth/logout" cannot suppress refresh.
  if (/(^|\/)auth\//.test(new URL(request.url, window.location.origin).pathname)) {
    return response;
  }

  let refreshed: boolean;
  try {
    refreshed = await refreshAccessToken();
  } catch (error) {
    if (error instanceof RefreshUnavailableError) {
      return response;
    }
    throw error;
  }
  if (!refreshed) {
    // A definitive refresh rejection means the session is dead. Transient
    // refresh failures return the original 401 above and leave auth intact.
    const returnTo = window.location.pathname + window.location.search + window.location.hash;
    if (!requestNativeAuth(returnTo)) {
      replaceWithLoginUrl(returnTo);
    }
    return response;
  }

  // A refreshed cookie can still race with a server-side revocation or a
  // route-specific authorization failure. Do not turn that single response
  // into a global logout; the auth-status query owns session invalidation.
  return fetch(request.clone());
}
