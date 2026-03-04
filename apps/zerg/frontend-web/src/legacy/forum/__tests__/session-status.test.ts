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
    // After implementation: blocked and needs_user are known states
    expect(normalizePresenceState("blocked")).toBe("blocked");
    expect(normalizePresenceState("needs_user")).toBe("needs_user");
    expect(normalizePresenceState(null)).toBeNull();
    expect(normalizePresenceState("totally_unknown")).toBeNull();
  });

  it("does not flag blocked/needs_user as unknown after implementation", () => {
    // These were previously unknown; after implementation they must be known
    expect(hasUnknownPresenceState("blocked")).toBe(false);
    expect(hasUnknownPresenceState("needs_user")).toBe(false);
    expect(hasUnknownPresenceState(null)).toBe(false);
    expect(hasUnknownPresenceState("future_state")).toBe(true);
  });

  it("treats thinking/running as active regardless of status", () => {
    expect(isSessionActive(makeSession({ status: "idle", presence_state: "running" }))).toBe(true);
    expect(isSessionActive(makeSession({ status: "completed", presence_state: "thinking" }))).toBe(true);
  });

  it("treats blocked and needs_user as active (session is live, user action needed)", () => {
    // blocked: waiting for permission — session is still running, just paused
    expect(isSessionActive(makeSession({ status: "idle", presence_state: "blocked" }))).toBe(true);
    expect(isSessionActive(makeSession({ status: "completed", presence_state: "blocked" }))).toBe(true);
    // needs_user: waiting for input — session is still running, just idle waiting
    expect(isSessionActive(makeSession({ status: "idle", presence_state: "needs_user" }))).toBe(true);
    expect(isSessionActive(makeSession({ status: "completed", presence_state: "needs_user" }))).toBe(true);
  });

  it("falls back to status when presence is absent or unknown", () => {
    expect(isSessionActive(makeSession({ status: "working", presence_state: null }))).toBe(true);
    // truly unknown state (not blocked/needs_user) → fall back to status
    expect(isSessionActive(makeSession({ status: "working", presence_state: "future_unknown" }))).toBe(true);
    expect(isSessionActive(makeSession({ status: "idle", presence_state: "future_unknown" }))).toBe(false);
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

  it("blocked and needs_user sort as active (sort key 0)", () => {
    const blocked = makeSession({ status: "idle", presence_state: "blocked" });
    const needsUser = makeSession({ status: "idle", presence_state: "needs_user" });
    const idle = makeSession({ status: "idle", presence_state: "idle" });

    expect(sessionActivitySortKey(blocked)).toBe(0);
    expect(sessionActivitySortKey(needsUser)).toBe(0);
    expect(sessionActivitySortKey(blocked)).toBeLessThan(sessionActivitySortKey(idle));
  });

  it("blocked and needs_user are not idle", () => {
    expect(isSessionIdle(makeSession({ status: "idle", presence_state: "blocked" }))).toBe(false);
    expect(isSessionIdle(makeSession({ status: "idle", presence_state: "needs_user" }))).toBe(false);
  });
});
