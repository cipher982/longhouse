export type KnownPresenceState = "thinking" | "running" | "idle" | "needs_user" | "blocked";

export type SessionActivitySnapshot = {
  status: string;
  ended_at: string | null;
  presence_state: string | null;
};

export function normalizePresenceState(state: string | null | undefined): KnownPresenceState | null {
  if (
    state === "thinking" ||
    state === "running" ||
    state === "idle" ||
    state === "needs_user" ||
    state === "blocked"
  ) {
    return state;
  }
  return null;
}

export function hasUnknownPresenceState(state: string | null | undefined): boolean {
  return state != null && normalizePresenceState(state) === null;
}

export function isSessionActive(session: SessionActivitySnapshot): boolean {
  const presenceState = normalizePresenceState(session.presence_state);
  // All non-idle known states mean the session is live (just paused differently)
  if (
    presenceState === "thinking" ||
    presenceState === "running" ||
    presenceState === "needs_user" ||
    presenceState === "blocked"
  ) {
    return true;
  }
  if (session.ended_at != null) {
    return false;
  }
  return session.status === "working" || session.status === "active";
}

export function isSessionIdle(session: SessionActivitySnapshot): boolean {
  const presenceState = normalizePresenceState(session.presence_state);
  if (presenceState === "idle") {
    return true;
  }
  if (isSessionActive(session)) {
    return false;
  }
  return session.status === "idle";
}

export function isSessionInactive(session: SessionActivitySnapshot): boolean {
  if (isSessionActive(session)) {
    return false;
  }
  if (session.ended_at != null) {
    return true;
  }
  return isSessionIdle(session) || session.status === "completed";
}

export function sessionActivitySortKey(session: SessionActivitySnapshot): number {
  if (isSessionActive(session)) {
    return 0;
  }
  if (isSessionIdle(session)) {
    return 1;
  }
  if (session.status === "completed" || session.ended_at != null) {
    return 2;
  }
  return 3;
}
