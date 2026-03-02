import { describe, expect, it } from "vitest";
import {
  hasUnknownPresenceState,
  isSessionActive,
  isSessionIdle,
  isSessionInactive,
  normalizePresenceState,
  sessionActivitySortKey,
  type SessionActivitySnapshot,
} from "../session-status";

function makeSession(overrides: Partial<SessionActivitySnapshot> = {}): SessionActivitySnapshot {
  return {
    status: "idle",
    ended_at: null,
    presence_state: null,
    ...overrides,
  };
}

describe("forum session status helpers", () => {
  it("normalizes known presence states", () => {
    expect(normalizePresenceState("thinking")).toBe("thinking");
    expect(normalizePresenceState("running")).toBe("running");
    expect(normalizePresenceState("idle")).toBe("idle");
    expect(normalizePresenceState("blocked")).toBeNull();
  });

  it("flags unsupported future states", () => {
    expect(hasUnknownPresenceState("blocked")).toBe(true);
    expect(hasUnknownPresenceState("needs_user")).toBe(true);
    expect(hasUnknownPresenceState(null)).toBe(false);
  });

  it("treats thinking/running as active regardless of status", () => {
    expect(isSessionActive(makeSession({ status: "idle", presence_state: "running" }))).toBe(true);
    expect(isSessionActive(makeSession({ status: "completed", presence_state: "thinking" }))).toBe(true);
  });

  it("falls back to status when presence is absent or unknown", () => {
    expect(isSessionActive(makeSession({ status: "working", presence_state: null }))).toBe(true);
    expect(isSessionActive(makeSession({ status: "working", presence_state: "blocked" }))).toBe(true);
    expect(isSessionActive(makeSession({ status: "idle", presence_state: "blocked" }))).toBe(false);
  });

  it("marks ended sessions inactive and completed", () => {
    const ended = makeSession({ status: "working", ended_at: "2026-03-01T00:00:00Z", presence_state: null });
    expect(isSessionActive(ended)).toBe(false);
    expect(isSessionInactive(ended)).toBe(true);
  });

  it("computes idle and sort priority consistently", () => {
    const active = makeSession({ status: "working", presence_state: "running" });
    const idle = makeSession({ status: "idle", presence_state: "idle" });
    const completed = makeSession({ status: "completed", ended_at: "2026-03-01T00:00:00Z" });

    expect(isSessionIdle(idle)).toBe(true);
    expect(isSessionIdle(active)).toBe(false);
    expect(sessionActivitySortKey(active)).toBeLessThan(sessionActivitySortKey(idle));
    expect(sessionActivitySortKey(idle)).toBeLessThan(sessionActivitySortKey(completed));
  });
});
