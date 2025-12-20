/**
 * UUID helpers
 *
 * Playwright E2E runs the app on `http://reverse-proxy` (Docker service name),
 * which is NOT a secure context. `crypto.randomUUID()` is secure-context gated and
 * can be undefined there, so we provide a safe fallback.
 */

function uuidFromBytes(bytes: Uint8Array): string {
  // RFC4122 v4 formatting: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0'));
  return (
    hex[0] +
    hex[1] +
    hex[2] +
    hex[3] +
    '-' +
    hex[4] +
    hex[5] +
    '-' +
    hex[6] +
    hex[7] +
    '-' +
    hex[8] +
    hex[9] +
    '-' +
    hex[10] +
    hex[11] +
    hex[12] +
    hex[13] +
    hex[14] +
    hex[15]
  );
}

/**
 * Generate a UUID v4 string.
 *
 * - Prefers `crypto.randomUUID()` when available.
 * - Falls back to `crypto.getRandomValues()` when possible.
 * - Final fallback uses `Math.random()` (non-crypto; only for client-side IDs).
 */
export function uuid(): string {
  const cryptoObj = (globalThis as any).crypto as Crypto | undefined;

  if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
    try {
      return cryptoObj.randomUUID();
    } catch {
      // Fall through to other strategies.
    }
  }

  if (cryptoObj && typeof cryptoObj.getRandomValues === 'function') {
    try {
      const bytes = new Uint8Array(16);
      cryptoObj.getRandomValues(bytes);
      // Set version (4) and variant (10xxxxxx).
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      return uuidFromBytes(bytes);
    } catch {
      // Fall through.
    }
  }

  // Non-crypto fallback (format preserved).
  let dt = Date.now();
  let perf = typeof performance !== 'undefined' && performance.now ? performance.now() * 1000 : 0;
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (dt > 0 ? (dt + Math.random() * 16) : (perf + Math.random() * 16)) % 16 | 0;
    if (dt > 0) dt = Math.floor(dt / 16);
    else perf = Math.floor(perf / 16);
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}
