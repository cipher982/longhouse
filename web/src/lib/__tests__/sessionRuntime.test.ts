import { describe, expect, it } from "vitest";
import type { TimelineRuntimeSession } from "../sessionRuntime";
import {
  isSessionClosed,
  resolveSessionOwnershipLabel,
  resolveSessionRuntimeState,
  resolveTimelineSignal,
  timelineSignalLabel,
} from "../sessionRuntime";
import { getRuntimeDisplayCopy, getRuntimeOutcomeLabel } from "../sessionUtils";
import { makeSessionStateFacts } from "../../test/sessionState";

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
  const display = overrides.runtime_display ?? makeRuntimeDisplay();
  const activity = display.tone === "thinking"
    ? "thinking"
    : display.tone === "running"
      ? "executing"
      : display.tone === "blocked"
        ? "blocked"
        : display.tone === "stalled"
          ? "stalled"
          : display.tone === "idle"
            ? "quiescent"
            : "unknown";
  return {
    ended_at: "2026-03-21T12:00:00Z",
    last_activity_at: "2026-03-21T12:00:00Z",
    timeline_anchor_at: "2026-03-21T12:00:00Z",
    capabilities: null,
    session_state: makeSessionStateFacts({
      closed: display.lifecycle === "closed",
      activity,
      access: display.control_path === "managed" ? "live_control" : "search_only",
      pendingInteraction: display.needs_attention,
    }),
    runtime_display: display,
    ...overrides,
  };
}

describe("resolveSessionRuntimeState", () => {
  it("ignores contradictory legacy aliases in favor of orthogonal facts", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        runtime_display: makeRuntimeDisplay({
          state: "running",
          tone: "running",
          headline: "Working",
          is_live: true,
          is_executing: true,
          control_path: "managed",
        }),
        session_state: makeSessionStateFacts({
          activity: "quiescent",
          access: "observe_only",
        }),
      }),
    );

    expect(runtime.presenceState).toBe("idle");
    expect(runtime.displayPhase).toBe("Idle");
    expect(runtime.isExecuting).toBe(false);
    expect(resolveSessionOwnershipLabel(runtime)).toBe("Unmanaged");
  });

  it("preserves the server-owned quiet tone for unknown activity", () => {
    const runtime = resolveSessionRuntimeState(makeSession({
      session_state: makeSessionStateFacts({ activity: "unknown" }),
    }));
    expect(runtime.displayPhase).toBe("Activity unknown");
    expect(runtime.tone).toBe("quiet");
  });

  it("reads canonical state facts instead of legacy display aliases", () => {
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

    expect(runtime.truthTier).toBe("fresh");
    expect(runtime.presenceState).toBe("running");
    expect(runtime.presenceTool).toBe("Shell");
    expect(runtime.displayPhase).toBe("Using Shell");
    expect(runtime.tone).toBe("running");
    expect(runtime.isExecuting).toBe(true);
    expect(resolveSessionOwnershipLabel(runtime)).toBe("Managed");
    expect(getRuntimeOutcomeLabel(runtime)).toBe("Using Shell");
    expect(getRuntimeDisplayCopy(runtime)).toEqual({
      headline: "Using Shell",
      detail: null,
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
    expect(isSessionClosed({ session_state: makeSessionStateFacts({ closed: true }) })).toBe(true);
    expect(getRuntimeOutcomeLabel(runtime)).toBe("Closed");
  });
});

describe("resolveTimelineSignal", () => {
  const sig = (overrides: Partial<TimelineRuntimeSession["runtime_display"]>, opts = {}) => {
    const tone = overrides.tone;
    const activity = (tone === "running" || tone === "thinking") && overrides.activity_recency === "live"
      ? (tone === "thinking" ? "thinking" : "executing")
      : tone === "blocked" || tone === "stalled"
        ? tone
        : "unknown";
    return resolveTimelineSignal({
      session_state: makeSessionStateFacts({
        closed: overrides.lifecycle === "closed",
        activity,
        pendingInteraction: overrides.needs_attention,
      }),
    }, opts);
  };

  it("closed wins over everything", () => {
    expect(sig({ lifecycle: "closed", needs_attention: true, tone: "running" })).toBe("closed");
  });

  it("needs_attention drives amber (not raw running tone)", () => {
    expect(sig({ needs_attention: true, tone: "running", activity_recency: "live" })).toBe("attention");
  });

  it("live thinking/running is working (teal, pulses)", () => {
    expect(sig({ tone: "running", activity_recency: "live" })).toBe("working");
    expect(sig({ tone: "thinking", activity_recency: "live" })).toBe("working");
  });

  it("transcript convergence never fabricates working activity", () => {
    expect(sig({ tone: "active", activity_recency: "live" })).toBe("unknown");
    expect(sig({ tone: "active", activity_recency: "stale" })).toBe("unknown");
  });

  it("stale running does NOT pulse and remains unknown", () => {
    expect(sig({ tone: "running", activity_recency: "stale" })).toBe("unknown");
  });

  it("blocked/stalled map to attention", () => {
    expect(sig({ tone: "stalled" })).toBe("attention");
    expect(sig({ tone: "blocked" })).toBe("attention");
  });

  it("idle is quiet", () => {
    expect(resolveTimelineSignal({
      session_state: makeSessionStateFacts({ activity: "quiescent" }),
      user_state: "active",
    })).toBe("quiet");
  });

  it("announces unknown activity honestly", () => {
    expect(timelineSignalLabel("unknown")).toBe("Activity unknown");
  });

  it("a global connectivity banner suppresses attention", () => {
    expect(sig({ needs_attention: true }, { connectivityHealthy: false })).toBe("quiet");
  });

  it("does not shout amber for a parked session (matches iOS isUserActive gate)", () => {
    const parked = resolveTimelineSignal({
      session_state: makeSessionStateFacts({ pendingInteraction: true }),
      user_state: "parked",
    });
    expect(parked).toBe("unknown");
    const active = resolveTimelineSignal({
      session_state: makeSessionStateFacts({ pendingInteraction: true }),
      user_state: "active",
    });
    expect(active).toBe("attention");
  });
});
