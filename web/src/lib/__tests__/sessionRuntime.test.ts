import { describe, expect, it } from "vitest";
import type { TimelineRuntimeSession } from "../sessionRuntime";
import {
  resolveSessionOwnershipLabel,
  resolveSessionRuntimeState,
  resolveSessionStatusLabel,
} from "../sessionRuntime";
import { getRuntimeDisplayCopy, getRuntimeOutcomeLabel } from "../sessionUtils";

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
  it("prefers server-derived runtime_display when present", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        status: "working",
        confidence: "live",
        runtime_source: "semantic",
        presence_state: "running",
        presence_tool: "bash",
        display_phase: "Running bash",
        runtime_display: {
          truth_tier: "managed-local",
          state: "running",
          tone: "running",
          headline: "Working",
          detail: "Running Shell",
          phase_label: "Running Shell",
          compact_tool_label: "Shell",
          is_live: true,
          is_executing: true,
          needs_attention: false,
          is_idle: false,
          heuristic_active: false,
          is_managed_local_truth: true,
          has_signal: true,
        },
      }),
    );

    expect(runtime.truthTier).toBe("managed-local");
    expect(runtime.displayPhase).toBe("Running Shell");
    expect(runtime.tone).toBe("running");
    expect(runtime.isManagedLocalTruth).toBe(true);
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Working",
      detail: "Running Shell",
    });
  });

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

  it("treats a fresh managed-local idle lease as ready even when ended_at is old", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        ended_at: "2026-03-21T10:00:00Z",
        capabilities: {
          live_control_available: true,
          host_reattach_available: true,
          reply_to_live_session_available: true,
        },
        status: "idle",
        confidence: "live",
        runtime_source: "semantic",
        presence_state: "idle",
        display_phase: "Idle",
      }),
    );

    expect(runtime.truthTier).toBe("managed-local");
    expect(runtime.heuristicActive).toBe(false);
    expect(runtime.isIdle).toBe(true);
    expect(runtime.tone).toBe("idle");
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Ready",
      detail: "Ready for next prompt",
    });
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

  it("uses Active for unmanaged outcome labels when fresh runtime evidence beats stale end time", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        ended_at: "2026-03-21T12:00:00Z",
        status: "working",
        confidence: "live",
        runtime_source: "semantic",
        presence_state: "running",
        presence_tool: "bash",
        display_phase: "Running bash",
      }),
    );

    expect(getRuntimeOutcomeLabel(runtime)).toBe("Active");
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
    // heuristicActive=true case: confidence=inferred triggers the heuristic path
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
        runtime_source: "managed_local_transport",
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

  it("shows not connected for managed-local with no presence and no heuristic signal", () => {
    // truthTier=managed-local but heuristicActive=false:
    // host_reattach_available + live confidence + managed_local_transport → managed-local tier
    // status=null avoids legacy progress status trigger, so heuristicActive stays false
    const runtime = resolveSessionRuntimeState(
      makeSession({
        ended_at: null,
        capabilities: {
          live_control_available: true,
          host_reattach_available: true,
          reply_to_live_session_available: true,
        },
        status: null,
        confidence: "live",
        runtime_source: "managed_local_transport",
        presence_state: null,
        display_phase: null,
      }),
    );

    expect(runtime.truthTier).toBe("managed-local");
    expect(runtime.heuristicActive).toBe(false);
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Not connected",
      detail: null,
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
      headline: "Not connected",
      detail: null,
    });
  });

  it("labels managed stale sessions as disconnected for card status", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        runtime_display: {
          truth_tier: "stale",
          state: null,
          tone: "inactive",
          headline: "Not connected",
          detail: null,
          phase_label: "Recent",
          compact_tool_label: null,
          is_live: false,
          is_executing: false,
          needs_attention: false,
          is_idle: false,
          heuristic_active: false,
          is_managed_local_truth: false,
          has_signal: true,
          control_path: "managed",
          activity_recency: "stale",
          lifecycle: "open",
          host_state: "unknown",
          terminal_reason: null,
        },
      }),
    );

    expect(resolveSessionOwnershipLabel(runtime)).toBe("Managed");
    expect(resolveSessionStatusLabel(runtime)).toBe("Disconnected");
  });

  it("labels unmanaged process-scanner matches as process seen", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        runtime_display: {
          truth_tier: "fresh",
          state: null,
          tone: "inactive",
          headline: "Inactive",
          detail: null,
          phase_label: "Recent",
          compact_tool_label: null,
          is_live: false,
          is_executing: false,
          needs_attention: false,
          is_idle: false,
          heuristic_active: false,
          is_managed_local_truth: false,
          has_signal: true,
          control_path: "unmanaged",
          activity_recency: "stale",
          lifecycle: "open",
          host_state: "online",
          terminal_reason: null,
        },
      }),
    );

    expect(resolveSessionOwnershipLabel(runtime)).toBe("Unmanaged");
    expect(resolveSessionStatusLabel(runtime)).toBe("Process seen");
  });
});
