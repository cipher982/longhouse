import { describe, expect, it } from "vitest";
import type { TimelineRuntimeSession } from "../sessionRuntime";
import { resolveSessionRuntimeState } from "../sessionRuntime";

function makeSession(overrides: Partial<TimelineRuntimeSession> = {}): TimelineRuntimeSession {
  return {
    ended_at: "2026-03-21T12:00:00Z",
    last_activity_at: "2026-03-21T12:00:00Z",
    timeline_anchor_at: "2026-03-21T12:00:00Z",
    execution_home: "legacy",
    managed_transport: null,
    ...overrides,
  };
}

describe("resolveSessionRuntimeState", () => {
  it("treats legacy working-without-presence as inferred progress", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        ended_at: null,
        status: "working",
        confidence: null,
        runtime_source: null,
        presence_state: null,
        display_phase: null,
      }),
    );

    expect(runtime.truthTier).toBe("inferred");
    expect(runtime.heuristicActive).toBe(true);
    expect(runtime.isLive).toBe(false);
    expect(runtime.displayPhase).toBe("Recent progress");
    expect(runtime.tone).toBe("inferred");
  });

  it("treats managed-local needs-user as trusted attention, not execution", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        execution_home: "managed_local",
        status: "active",
        confidence: "live",
        runtime_source: "managed_local_transport",
        presence_state: "needs_user",
        display_phase: "Needs you",
      }),
    );

    expect(runtime.truthTier).toBe("managed-local");
    expect(runtime.needsAttention).toBe(true);
    expect(runtime.isLive).toBe(false);
    expect(runtime.isExecuting).toBe(false);
    expect(runtime.heuristicActive).toBe(false);
    expect(runtime.tone).toBe("needs-user");
    expect(runtime.displayPhase).toBe("Needs you");
  });
});
