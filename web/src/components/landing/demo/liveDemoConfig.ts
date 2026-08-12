/**
 * Live free-type demo endpoints.
 *
 * There is no token, flag, or env var here on purpose. Abuse and spend are
 * bounded server-side in the worker (per-visitor hourly cap, global daily cap,
 * per-session request cap, clamped max_tokens, pinned model), which is the
 * right place for them: nobody should have to carry a credential to use the
 * landing page, and a client-side gate was never protection anyway.
 */

const WORKER_BASE =
  "https://freetype-phase1.drose-agents.workers.dev/__phase1_free_type_8f2c1a7e";

export function sessionUrl(): string {
  return `${WORKER_BASE}/api/session`;
}

export function terminalUrl(sessionId: string): string {
  const wsBase = WORKER_BASE.replace(/^https:/, "wss:");
  return `${wsBase}/ws?session=${encodeURIComponent(sessionId)}`;
}

/** Geometry is fixed to match the sandbox PTY; mismatched cols corrupt TUIs. */
export const LIVE_COLS = 64;
export const LIVE_ROWS = 20;
