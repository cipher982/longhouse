import { describe, expect, it } from "vitest";
import type { TimelineRuntimeSession } from "../sessionRuntime";
import {
  isSessionClosed,
  resolveSessionOwnershipLabel,
  resolveSessionRuntimeState,
} from "../sessionRuntime";
import { getRuntimeDisplayCopy, getRuntimeOutcomeLabel } from "../sessionUtils";

function makeRuntimeDisplay(
  overrides: Partial<TimelineRuntimeSession["runtime_display"]> = {},
): TimelineRuntimeSession["runtime_display"] {
  return {
    truth_tier: "managed-local",
    signal_tier: "phase_signal",
    state: null,
    tone: "inactive",
    headline: "Inactive",
    detail: null,
    phase_label: "Inactive",
    compact_tool_label: null,
    is_live: false,
    is_executing: false,
    needs_attention: false,
    is_idle: false,
    is_stalled: false,
    is_managed_local_truth: true,
    has_signal: true,
    control_path: "managed",
    activity_recency: "stale",
    lifecycle: "open",
    host_state: "unknown",
    terminal_reason: null,
    ...overrides,
  };
}

function makeSession(overrides: Partial<TimelineRuntimeSession> = {}): TimelineRuntimeSession {
  return {
    ended_at: "2026-03-21T12:00:00Z",
    last_activity_at: "2026-03-21T12:00:00Z",
    timeline_anchor_at: "2026-03-21T12:00:00Z",
    capabilities: null,
    runtime_display: makeRuntimeDisplay(),
    ...overrides,
  };
}

describe("resolveSessionRuntimeState", () => {
  it("reads backend runtime_display directly", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        status: "working",
        runtime_source: "managed_local_transport",
        confidence: "live",
        presence_state: "idle",
        runtime_display: makeRuntimeDisplay({
          truth_tier: "managed-local",
          state: "running",
          tone: "running",
          headline: "Working",
          detail: "Using Shell",
          phase_label: "Using Shell",
          compact_tool_label: "Shell",
          is_live: true,
          is_executing: true,
          is_idle: false,
          control_path: "managed",
          activity_recency: "live",
        }),
      }),
    );

    expect(runtime.truthTier).toBe("managed-local");
    expect(runtime.presenceState).toBe("running");
    expect(runtime.presenceTool).toBe("Shell");
    expect(runtime.displayPhase).toBe("Using Shell");
    expect(runtime.tone).toBe("running");
    expect(runtime.isExecuting).toBe(true);
    expect(resolveSessionOwnershipLabel(runtime)).toBe("Managed");
    expect(getRuntimeOutcomeLabel(runtime)).toBe("Working");
    expect(getRuntimeDisplayCopy(runtime)).toEqual({
      headline: "Working",
      detail: "Using Shell",
    });
  });

  it("uses backend closed lifecycle and tone without client reinterpretation", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        runtime_display: makeRuntimeDisplay({
          truth_tier: "stale",
          state: null,
          tone: "closed",
          headline: "Closed",
          detail: null,
          phase_label: "Closed",
          is_live: false,
          is_executing: false,
          needs_attention: false,
          is_idle: true,
          is_stalled: false,
          lifecycle: "closed",
          terminal_reason: "process_gone",
        }),
      }),
    );

    expect(runtime.presenceState).toBeNull();
    expect(runtime.isLive).toBe(false);
    expect(runtime.isExecuting).toBe(false);
    expect(runtime.needsAttention).toBe(false);
    expect(runtime.isIdle).toBe(true);
    expect(runtime.displayPhase).toBe("Closed");
    expect(runtime.tone).toBe("closed");
    expect(isSessionClosed({ runtime_display: runtime.runtimeDisplay })).toBe(true);
    expect(getRuntimeOutcomeLabel(runtime)).toBe("Closed");
  });
});
