import { describe, expect, it } from "vitest";
import type { TimelineRuntimeSession } from "../sessionRuntime";
import {
  resolveSessionOwnershipLabel,
  resolveSessionRuntimeState,
  isSessionClosed,
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
          detail: "Using Shell",
          phase_label: "Using Shell",
          compact_tool_label: "Shell",
          is_live: true,
          is_executing: true,
          needs_attention: false,
          is_idle: false,
          is_managed_local_truth: true,
          has_signal: true,
        },
      }),
    );

    expect(runtime.truthTier).toBe("managed-local");
    expect(runtime.displayPhase).toBe("Using Shell");
    expect(runtime.tone).toBe("running");
    expect(runtime.isManagedLocalTruth).toBe(true);
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Working",
      detail: "Using Shell",
    });
  });

  it("does not resurrect stale top-level attention when runtime_display clears state", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        status: "active",
        presence_state: "needs_user",
        display_phase: "Needs you",
        runtime_display: makeRuntimeDisplay({
          state: null,
          tone: "inactive",
          headline: "Not connected",
          detail: null,
          phase_label: "Recent",
          needs_attention: false,
          is_executing: false,
          is_live: false,
          lifecycle: "open",
          activity_recency: "stale",
        }),
      }),
    );

    expect(runtime.presenceState).toBeNull();
    expect(runtime.needsAttention).toBe(false);
    expect(runtime.isExecuting).toBe(false);
    expect(runtime.tone).toBe("inactive");
  });

  it("does not convert status-only progress into liveness", () => {
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

    expect(runtime.truthTier).toBe("none");
    expect(runtime.isLive).toBe(false);
    expect(runtime.displayPhase).toBe("Inactive");
    expect(runtime.tone).toBe("inactive");
  });

  it("treats managed-local needs-user as idle, not attention or execution", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        capabilities: {
          live_control_available: false,
          host_reattach_available: true,
          reply_to_live_session_available: false,
        },
        status: "idle",
        confidence: "live",
        runtime_source: "managed_local_transport",
        presence_state: "needs_user",
        display_phase: "Idle",
      }),
    );

    expect(runtime.truthTier).toBe("managed-local");
    expect(runtime.needsAttention).toBe(false);
    expect(runtime.isLive).toBe(false);
    expect(runtime.isExecuting).toBe(false);
    expect(runtime.tone).toBe("idle");
    expect(runtime.displayPhase).toBe("Idle");
  });

  it("treats a fresh managed-local idle lease as idle even when ended_at is old", () => {
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
    expect(runtime.isIdle).toBe(true);
    expect(runtime.tone).toBe("idle");
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Idle",
      detail: "Waiting for next prompt",
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
      detail: "Using Shell",
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

  it("collapses managed-local blocked state into permission copy", () => {
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
      headline: "Needs permission",
      detail: "Approval needed • Edit",
    });
  });

  it("treats managed-local progress without a phase as not connected", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        ended_at: null,
        capabilities: {
          live_control_available: true,
          host_reattach_available: true,
          reply_to_live_session_available: true,
        },
        status: "active",
        confidence: "stale",
        runtime_source: "managed_local_transport",
        presence_state: null,
        display_phase: "Recent",
      }),
    );

    expect(runtime.truthTier).toBe("stale");
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Not connected",
      detail: null,
    });
  });

  it("shows not connected for managed-local with no presence signal", () => {
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
  });

  it("labels server-detected managed stalls without treating them as active work", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        presence_state: "thinking",
        runtime_display: {
          truth_tier: "stale",
          state: "stalled",
          tone: "stalled",
          headline: "Stalled",
          detail: "No recent managed-session progress",
          phase_label: "Stalled",
          compact_tool_label: null,
          is_live: false,
          is_executing: false,
          needs_attention: true,
          is_idle: false,
          is_stalled: true,
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

    expect(runtime.presenceState).toBe("stalled");
    expect(runtime.isStalled).toBe(true);
    expect(runtime.isExecuting).toBe(false);
    expect(runtime.tone).toBe("stalled");
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Stalled",
      detail: "No recent managed-session progress",
    });
  });

  it("keeps stale unmanaged sessions stale even when the host is online", () => {
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
  });

  it("does not promote unmanaged online hosts into session status", () => {
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
          is_managed_local_truth: false,
          has_signal: true,
          control_path: "unmanaged",
          activity_recency: "none",
          lifecycle: "open",
          host_state: "online",
          terminal_reason: null,
        },
      }),
    );

    expect(resolveSessionOwnershipLabel(runtime)).toBe("Unmanaged");
  });

  it("lets closed lifecycle suppress stale attention flags everywhere", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        status: "active",
        presence_state: "needs_user",
        runtime_display: makeRuntimeDisplay({
          state: "needs_user",
          tone: "idle",
          headline: "Idle",
          detail: "Waiting for next prompt",
          phase_label: "Idle",
          needs_attention: true,
          is_live: true,
          is_executing: true,
          is_stalled: true,
          lifecycle: "closed",
          terminal_reason: "process_gone",
        }),
      }),
    );

    expect(runtime.presenceState).toBeNull();
    expect(runtime.isLive).toBe(false);
    expect(runtime.isExecuting).toBe(false);
    expect(runtime.needsAttention).toBe(false);
    expect(runtime.isStalled).toBe(false);
    expect(runtime.isIdle).toBe(true);
    expect(runtime.displayPhase).toBe("Closed");
    expect(runtime.tone).toBe("closed");
    expect(getRuntimeOutcomeLabel(runtime)).toBe("Closed");
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Closed",
      detail: null,
    });
  });

  it("renders closed lifecycle generically regardless of terminal_reason metadata", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        status: "active",
        presence_state: "needs_user",
        runtime_display: makeRuntimeDisplay({
          state: "needs_user",
          tone: "idle",
          headline: "Completed",
          phase_label: "Completed",
          needs_attention: true,
          is_live: true,
          is_executing: true,
          lifecycle: "closed",
          terminal_reason: "terminal_disconnected",
        }),
      }),
    );

    expect(runtime.presenceState).toBeNull();
    expect(runtime.isLive).toBe(false);
    expect(runtime.isExecuting).toBe(false);
    expect(runtime.needsAttention).toBe(false);
    expect(runtime.displayPhase).toBe("Closed");
    expect(runtime.tone).toBe("closed");
    expect(getRuntimeOutcomeLabel(runtime)).toBe("Closed");
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Closed",
      detail: null,
    });
  });

  it("renders managed phase facts with the observed phase tone", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        presence_state: "running",
        runtime_display: makeRuntimeDisplay({
          state: "running",
          headline: "Working",
          phase_label: "Using Shell",
          activity_recency: "live",
        }),
        runtime_facts: makeRuntimeFacts({
          control_path: "managed",
          host: {
            state: "online",
            last_seen_at: "2026-03-21T12:00:00Z",
            source: "machine_heartbeat",
          },
          phase: {
            kind: "running",
            tool: "shell",
            source: "codex_bridge",
            observed_at: "2026-03-21T12:00:05Z",
            expires_at: "2026-03-21T12:15:05Z",
          },
          lifecycle: {
            state: "open",
            reason: "phase_observed",
            observed_at: "2026-03-21T12:00:05Z",
          },
        }),
      }),
    );

    expect(resolveSessionOwnershipLabel(runtime)).toBe("Managed");
    expect(runtime.factStatus).toMatchObject({
      label: "Using Shell",
      tone: "running",
      seenAtPrefix: "Updated",
    });
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Using Shell",
      detail: null,
    });
  });

  it("lets server display show transcript sync while raw facts still report idle", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        status: "idle",
        presence_state: "idle",
        runtime_display: makeRuntimeDisplay({
          state: "syncing_transcript",
          tone: "active",
          headline: "Syncing",
          detail: "Waiting for transcript",
          phase_label: "Syncing transcript",
          is_idle: false,
          is_executing: false,
          is_live: false,
          control_path: "managed",
          activity_recency: "live",
        }),
        runtime_facts: makeRuntimeFacts({
          control_path: "managed",
          phase: {
            kind: "idle",
            tool: null,
            source: "claude_hook",
            observed_at: "2026-03-21T12:00:05Z",
            expires_at: "2026-03-21T12:10:05Z",
          },
          lifecycle: {
            state: "open",
            reason: "phase_observed",
            observed_at: "2026-03-21T12:00:05Z",
          },
        }),
      }),
    );

    expect(runtime.factStatus).toBeNull();
    expect(runtime.displayPhase).toBe("Syncing transcript");
    expect(runtime.tone).toBe("active");
    expect(runtime.isIdle).toBe(false);
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Syncing",
      detail: "Waiting for transcript",
    });
  });

  it("maps runtime fact phases to visual tones without timestamp inference", () => {
    const cases: Array<[string, string, string]> = [
      ["thinking", "Thinking", "thinking"],
      ["running", "Using Shell", "running"],
      ["blocked", "Blocked Shell", "blocked"],
      ["stalled", "Stalled", "stalled"],
      ["idle", "Idle", "idle"],
      ["needs_user", "Idle", "idle"],
      ["reviewing", "Reviewing", "inactive"],
    ];

    for (const [kind, label, tone] of cases) {
      const runtime = resolveSessionRuntimeState(
        makeSession({
          runtime_facts: makeRuntimeFacts({
            phase: {
              kind,
              tool: "bash",
              source: "managed_local_transport",
              observed_at: "2026-03-21T12:00:05Z",
              expires_at: "2026-03-21T12:15:05Z",
            },
          }),
        }),
      );

      expect(runtime.factStatus).toMatchObject({
        label,
        tone,
        seenAtPrefix: "Updated",
      });
      expect(runtime.isExecuting).toBe(false);
      expect(runtime.isLive).toBe(false);
    }
  });

  it("renders unmanaged process observations as process-visible facts", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        runtime_display: makeRuntimeDisplay({
          control_path: "unmanaged",
          activity_recency: "live",
          headline: "Active",
        }),
        runtime_facts: makeRuntimeFacts({
          control_path: "unmanaged",
          process_state: "running",
          host: {
            state: "online",
            last_seen_at: "2026-03-21T12:00:00Z",
            source: "machine_heartbeat",
          },
          process: {
            status: "observed",
            pid: 123,
            process_start_time: null,
            observed_at: "2026-03-21T12:00:05Z",
            last_seen_at: "2026-03-21T12:00:06Z",
            source_mtime: null,
            source_path: "/tmp/session.jsonl",
            reason: null,
            source: "machine_process_scan",
          },
          lifecycle: {
            state: "open",
            reason: "process_observed",
            observed_at: "2026-03-21T12:00:05Z",
          },
        }),
      }),
    );

    expect(resolveSessionOwnershipLabel(runtime)).toBe("Unmanaged");
    expect(getRuntimeOutcomeLabel(runtime)).toBe("Running");
    expect(runtime.factStatus).toMatchObject({
      label: "Running",
      tone: "inactive",
      seenAtPrefix: "Verified",
    });
  });

  it("renders transcript-only facts as no live signal", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        runtime_display: makeRuntimeDisplay({
          control_path: "unmanaged",
          activity_recency: "recent",
          headline: "Active",
        }),
        runtime_facts: makeRuntimeFacts({
          control_path: "unmanaged",
          activity: {
            last_transcript_at: "2026-03-21T12:00:00Z",
            last_runtime_signal_at: null,
            last_progress_at: null,
          },
          lifecycle: {
            state: "unknown",
            reason: null,
            observed_at: null,
          },
        }),
      }),
    );

    expect(getRuntimeOutcomeLabel(runtime)).toBe("No live signal");
  });

  it("renders unknown host facts as no live signal", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        runtime_display: makeRuntimeDisplay({
          control_path: "managed",
          activity_recency: "stale",
          headline: "Disconnected",
        }),
        runtime_facts: makeRuntimeFacts({
          control_path: "managed",
          activity: {
            last_transcript_at: null,
            last_runtime_signal_at: "2026-03-21T11:00:00Z",
            last_progress_at: null,
          },
          host: {
            state: "unknown",
            last_seen_at: null,
            source: null,
          },
          lifecycle: {
            state: "unknown",
            reason: null,
            observed_at: null,
          },
        }),
      }),
    );

    expect(runtime.factStatus).toMatchObject({
      label: "No live signal",
      tone: "inactive",
      seenAt: "2026-03-21T11:00:00Z",
      seenAtPrefix: "Last signal",
    });
  });

  it("renders closed facts as closed rather than completed", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        runtime_display: makeRuntimeDisplay({
          lifecycle: "open",
          headline: "Working",
          phase_label: "Using Shell",
        }),
        runtime_facts: makeRuntimeFacts({
          control_path: "managed",
          lifecycle: {
            state: "closed",
            reason: "session_ended",
            observed_at: "2026-03-21T12:00:00Z",
          },
        }),
      }),
    );

    expect(runtime.tone).toBe("closed");
    expect(getRuntimeOutcomeLabel(runtime)).toBe("Closed");
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Closed",
      detail: null,
    });
  });

  it("renders closed lifecycle facts with generic label regardless of reason metadata", () => {
    const runtime = resolveSessionRuntimeState(
      makeSession({
        runtime_display: makeRuntimeDisplay({
          lifecycle: "open",
          headline: "Working",
          phase_label: "Using Shell",
        }),
        runtime_facts: makeRuntimeFacts({
          control_path: "managed",
          process_state: "closed",
          lifecycle: {
            state: "closed",
            reason: "terminal_disconnected",
            observed_at: "2026-03-21T12:00:00Z",
          },
        }),
      }),
    );

    expect(runtime.displayPhase).toBe("Closed");
    expect(runtime.tone).toBe("closed");
    expect(getRuntimeOutcomeLabel(runtime)).toBe("Closed");
    expect(getRuntimeDisplayCopy(runtime, { managedLocal: true })).toEqual({
      headline: "Closed",
      detail: null,
    });
  });

  it("does not promote unknown facts through legacy terminal hints", () => {
    expect(
      isSessionClosed({
        terminal_state: "finished",
        runtime_facts: makeRuntimeFacts({
          lifecycle: {
            state: "unknown",
            reason: null,
            observed_at: null,
          },
        }),
      }),
    ).toBe(false);
  });
});
