import { describe, expect, it } from "vitest";
import type { TimelineRuntimeSession } from "../sessionRuntime";
import { resolveSessionRuntimeState } from "../sessionRuntime";
import { getRuntimeDisplayCopy } from "../sessionUtils";

function makeSession(overrides: Partial<TimelineRuntimeSession> = {}): TimelineRuntimeSession {
  return {
    ended_at: "2026-03-21T12:00:00Z",
    last_activity_at: "2026-03-21T12:00:00Z",
    timeline_anchor_at: "2026-03-21T12:00:00Z",
    capabilities: null,
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
        capabilities: {
          live_control_available: false,
          host_reattach_available: true,
          reply_to_live_session_available: false,
        },
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

  it("collapses managed-local execution into Working with a richer detail label", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        capabilities: {
          live_control_available: true,
          host_reattach_available: true,
          reply_to_live_session_available: true,
        },
        status: "working",
        confidence: "live",
        runtime_source: "semantic",
        presence_state: "running",
        presence_tool: "bash",
        display_phase: "Running bash",
      }),
    );

    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Working",
      detail: "Running Shell",
    });
  });

  it("collapses managed-local blocked state into Waiting for you with approval detail", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        capabilities: {
          live_control_available: true,
          host_reattach_available: true,
          reply_to_live_session_available: true,
        },
        status: "active",
        confidence: "live",
        runtime_source: "semantic",
        presence_state: "blocked",
        presence_tool: "edit",
        display_phase: "Blocked on edit",
      }),
    );

    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Waiting for you",
      detail: "Approval needed • Edit",
    });
  });

  it("treats managed-local inferred progress as working instead of ready", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        ended_at: null,
        capabilities: {
          live_control_available: true,
          host_reattach_available: true,
          reply_to_live_session_available: true,
        },
        status: "active",
        confidence: "inferred",
        runtime_source: "semantic",
        presence_state: null,
        display_phase: "Recent progress",
      }),
    );

    expect(runtime.truthTier).toBe("managed-local");
    expect(runtime.heuristicActive).toBe(true);
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Working",
      detail: "Recent progress",
    });
  });

  it("marks stale managed-local state as unavailable instead of pretending it is active", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        capabilities: {
          live_control_available: false,
          host_reattach_available: true,
          reply_to_live_session_available: false,
        },
        status: "idle",
        confidence: "stale",
        runtime_source: "fallback",
        presence_state: null,
        display_phase: null,
      }),
    );

    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "State unavailable",
      detail: "Waiting for live signal",
    });
  });
});
