/**
 * Live free-type demo: gate + endpoint config.
 *
 * The gate is a capability token the operator supplies in the URL
 * (`?livedemo=<token>`). It is deliberately NOT baked into the bundle and NOT
 * derived from `config.demoMode`, which controls unrelated app behaviour.
 *
 * The token is both the client's visibility switch and the worker's
 * authorization: the worker independently validates it before creating a
 * sandbox, so shipping this chunk does not ship a money-spending endpoint.
 * A query parameter alone would be visibility, never access control.
 */

const WORKER_BASE =
  "https://freetype-phase1.drose-agents.workers.dev/__phase1_free_type_8f2c1a7e";

export const LIVE_DEMO_QUERY_PARAM = "livedemo";

export function liveDemoToken(search: string = window.location.search): string | null {
  const token = new URLSearchParams(search).get(LIVE_DEMO_QUERY_PARAM);
  return token && token.trim() ? token.trim() : null;
}

export function isLiveDemoEnabled(search?: string): boolean {
  return liveDemoToken(search) !== null;
}

export function sessionUrl(token: string): string {
  return `${WORKER_BASE}/api/session?token=${encodeURIComponent(token)}`;
}

export function terminalUrl(sessionId: string, token: string): string {
  const wsBase = WORKER_BASE.replace(/^https:/, "wss:");
  return `${wsBase}/ws?session=${encodeURIComponent(sessionId)}&token=${encodeURIComponent(token)}`;
}

/** Geometry is fixed to match the sandbox PTY; mismatched cols corrupt TUIs. */
export const LIVE_COLS = 64;
export const LIVE_ROWS = 20;
