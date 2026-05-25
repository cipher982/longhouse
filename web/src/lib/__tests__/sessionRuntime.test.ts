import { describe, expect, it } from "vitest";
import type { TimelineRuntimeSession } from "../sessionRuntime";
import {
  isSessionClosed,
  resolveSessionOwnershipLabel,
  resolveSessionRuntimeState,
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

function makeRuntimeDisplay(
  overrides: Partial<NonNullable<TimelineRuntimeSession["runtime_display"]>> = {},
): NonNullable<TimelineRuntimeSession["runtime_display"]> {
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

function makeRuntimeFacts(
  overrides: Partial<NonNullable<TimelineRuntimeSession["runtime_facts"]>> = {},
): NonNullable<TimelineRuntimeSession["runtime_facts"]> {
  return {
    control_path: "managed",
    control: {
      state: "online",
      reason: null,
      source: "machine_heartbeat",
      last_seen_at: "2026-03-21T12:00:00Z",
      expires_at: "2026-03-21T12:15:00Z",
      transport: "claude_channel_bridge",
    },
    process_state: "unknown",
    host: {
      state: "unknown",
      last_seen_at: null,
      source: null,
    },
    process: {
      status: "unknown",
      pid: null,
      process_start_time: null,
      observed_at: null,
      last_seen_at: null,
      source_mtime: null,
      source_path: null,
      reason: null,
      source: null,
    },
    phase: {
      kind: null,
      tool: null,
      source: null,
      observed_at: null,
      expires_at: null,
    },
    activity: {
      last_transcript_at: null,
      last_runtime_signal_at: null,
      last_progress_at: null,
    },
    lifecycle: {
      state: "unknown",
      reason: null,
      observed_at: null,
    },
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
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Working",
      detail: "Using Shell",
    });
  });

  it("does not let runtime_facts override runtime_display presentation", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        runtime_display: makeRuntimeDisplay({
          state: "idle",
          tone: "idle",
          headline: "Idle",
          detail: "Waiting for next prompt",
          phase_label: "Idle",
          is_idle: true,
          is_executing: false,
        }),
        runtime_facts: makeRuntimeFacts({
          lifecycle: {
            state: "closed",
            reason: "session_ended",
            observed_at: "2026-03-21T12:00:00Z",
          },
          phase: {
            kind: "running",
            tool: "bash",
            source: "managed_local_transport",
            observed_at: "2026-03-21T12:00:00Z",
            expires_at: "2026-03-21T12:15:00Z",
          },
        }),
      }),
    );

    expect(runtime.displayPhase).toBe("Idle");
    expect(runtime.tone).toBe("idle");
    expect(runtime.factStatus).toBeNull();
    expect(getRuntimeOutcomeLabel(runtime)).toBe("Idle");
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
    expect(isSessionClosed({ terminal_state: null, runtime_display: runtime.runtimeDisplay })).toBe(true);
    expect(getRuntimeOutcomeLabel(runtime)).toBe("Closed");
  });

  it("treats missing runtime_display as inert compatibility state", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        status: "working",
        presence_state: "running",
        runtime_facts: makeRuntimeFacts({
          phase: {
            kind: "running",
            tool: "bash",
            source: "managed_local_transport",
            observed_at: "2026-03-21T12:00:00Z",
            expires_at: "2026-03-21T12:15:00Z",
          },
        }),
      }),
    );

    expect(runtime.truthTier).toBe("none");
    expect(runtime.presenceState).toBeNull();
    expect(runtime.displayPhase).toBe("Inactive");
    expect(runtime.isLive).toBe(false);
    expect(runtime.factStatus).toBeNull();
    expect(getRuntimeOutcomeLabel(runtime)).toBe("Inactive");
  });

  it("keeps terminal_state as the legacy closed fallback", () => {
    expect(
      isSessionClosed({
        terminal_state: "process_gone",
        runtime_display: null,
      }),
    ).toBe(true);
    expect(
      isSessionClosed({
        terminal_state: "finished",
        runtime_display: null,
      }),
    ).toBe(false);
  });
});
