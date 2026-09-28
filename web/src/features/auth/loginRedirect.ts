/**
 * Safe `return_to` helpers for the /login route.
 *
 * Rules:
 * - Must be a path-only string (no scheme, no host).
 * - Must start with "/" but NOT with "//" (scheme-relative open-redirect).
 * - Backslash-prefixed values are rejected (IE-era open-redirect trick).
 * - Falls back to /timeline on any invalid input.
 */

export const DEFAULT_RETURN_TO = "/timeline";

/**
 * Sanitize a `return_to` value before trusting it as a redirect destination.
 * Returns the sanitized path or DEFAULT_RETURN_TO if the value is unsafe.
 */
export function sanitizeReturnTo(raw: string | null | undefined): string {
  if (!raw) return DEFAULT_RETURN_TO;

  // Reject anything that is not a plain path
  if (!raw.startsWith("/")) return DEFAULT_RETURN_TO;
  // Reject scheme-relative: //evil.com
  if (raw.startsWith("//")) return DEFAULT_RETURN_TO;
  // Reject backslash-prefixed (Windows open-redirect trick: /\evil.com)
  if (raw.startsWith("/\\")) return DEFAULT_RETURN_TO;

  // Verify it parses as a same-origin URL
  try {
    const url = new URL(raw, window.location.origin);
    if (url.origin !== window.location.origin) return DEFAULT_RETURN_TO;
    // Return only path + search + hash — never host/scheme
    return url.pathname + url.search + url.hash;
  } catch {
    return DEFAULT_RETURN_TO;
  }
}

/**
 * Build the /login URL with a safe return_to param from the current location.
 */
export function buildLoginUrl(returnTo: string): string {
  const safe = sanitizeReturnTo(returnTo);
  return `/login?return_to=${encodeURIComponent(safe)}`;
}

export function replaceWithLoginUrl(returnTo: string): void {
  window.location.replace(buildLoginUrl(returnTo));
}

// ---------------------------------------------------------------------------
// Timeline session id extraction
// ---------------------------------------------------------------------------
// The login page can show whose session a visitor was trying to reach when
// the return_to destination is /timeline/<uuid>. The UUID pattern is the same
// one the server uses for short-link lookup; we extract the full id here so
// callers can pass the 8-hex-char prefix to /s/<prefix>/preview.

const TIMELINE_SESSION_PATTERN =
  /^\/timeline\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\/?$/i;

/**
 * If `returnTo` points at a /timeline/<uuid> session, return the full UUID.
 * Returns null for any other path, malformed UUID, or unsafe input.
 */
export function extractTimelineSessionId(returnTo: string): string | null {
  if (!returnTo || !returnTo.startsWith("/")) return null;
  const path = returnTo.split("?")[0].split("#")[0];
  const match = TIMELINE_SESSION_PATTERN.exec(path);
  return match ? match[1].toLowerCase() : null;
}

/**
 * The 8-hex-char prefix used by the server's /s/<prefix> routes. Returns null
 * for any UUID that doesn't start with 8 hex characters.
 */
export function shortSessionPrefix(sessionId: string): string | null {
  if (!sessionId) return null;
  const head = sessionId.split("-")[0] ?? "";
  return /^[0-9a-f]{8}$/i.test(head) ? head.toLowerCase() : null;
}
