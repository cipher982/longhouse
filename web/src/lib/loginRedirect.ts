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
