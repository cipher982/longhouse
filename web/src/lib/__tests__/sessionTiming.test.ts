import { describe, expect, it } from "vitest";
import type { AgentSession, AgentSessionTurn } from "../../services/api/agents";
import {
  formatElapsedCounter,
  getActiveSessionTurn,
  getRuntimeElapsedLabel,
} from "../sessionTiming";

function makeSession(overrides: Partial<AgentSession> = {}): AgentSession {
  return {
    id: "session-timing",
    provider: "codex",
    project: "zerg",
    device_id: "cinder",
    environment: "development",
    cwd: "/Users/davidrose/git/zerg",
    git_repo: "git@github.com:cipher982/longhouse.git",
    git_branch: "main",
    started_at: "2026-03-22T22:00:00Z",
    ended_at: null,
    last_activity_at: "2026-03-22T22:04:30Z",
    user_messages: 1,
    assistant_messages: 1,
    tool_calls: 1,
    summary: null,
    summary_title: null,
    first_user_message: null,
    thread_root_session_id: "session-timing",
    thread_head_session_id: "session-timing",
    thread_continuation_count: 1,
    continued_from_session_id: null,
    continuation_kind: "local",
    origin_label: "On this Mac",
    home_label: "On this Mac",
    branched_from_event_id: null,
    is_writable_head: true,
    control: null,
    capabilities: null,
    loop_mode: "assist",
    runtime_display: {
      truth_tier: "none",
      signal_tier: "none",
      state: null,
      tone: "inactive",
      headline: "Inactive",
      detail: null,
      phase_label: "Inactive",
      compact_tool_label: null,
      is_live: false,
      is_executing: false,
      needs_attention: false,
      is_idle: true,
      is_stalled: false,
      is_managed_local_truth: false,
      has_signal: false,
      control_path: "unmanaged",
      activity_recency: "stale",
      lifecycle: "open",
      host_state: null,
      terminal_reason: null,
    },
    ...overrides,
  } as AgentSession;
}

function makeTurn(overrides: Partial<AgentSessionTurn> = {}): AgentSessionTurn {
  return {
    id: 1,
    session_id: "session-timing",
    request_id: "req-1",
    state: "active",
    terminal_phase: null,
    error_code: null,
    user_event_id: null,
    durable_assistant_event_id: null,
    baseline_event_id: null,
    baseline_observation_cursor: null,
    user_submitted_at: "2026-03-22T22:03:45Z",
    send_accepted_at: "2026-03-22T22:03:46Z",
    active_phase_observed_at: "2026-03-22T22:03:47Z",
    terminal_at: null,
    durable_at: null,
    created_at: "2026-03-22T22:03:45Z",
    updated_at: "2026-03-22T22:03:47Z",
    ...overrides,
  };
}

describe("sessionTiming", () => {
  it("formats elapsed counters as mm:ss under one hour", () => {
    expect(
      formatElapsedCounter(
        "2026-03-22T22:00:00Z",
        "2026-03-22T22:04:30Z",
        Date.parse("2026-03-22T22:04:30Z"),
      ),
    ).toBe("04:30");
  });

  it("formats elapsed counters as h:mm:ss at one hour or more", () => {
    expect(
      formatElapsedCounter(
        "2026-03-22T20:00:00Z",
        "2026-03-22T21:02:03Z",
        Date.parse("2026-03-22T21:02:03Z"),
      ),
    ).toBe("1:02:03");
  });

  it("picks the first active turn from the newest-first turn list", () => {
    const turns = [
      makeTurn({
        id: 3,
        state: "durable",
        user_submitted_at: "2026-03-22T22:04:00Z",
        durable_at: "2026-03-22T22:04:10Z",
      }),
      makeTurn({
        id: 2,
        state: "active",
        user_submitted_at: "2026-03-22T22:03:45Z",
      }),
      makeTurn({
        id: 1,
        state: "failed",
        user_submitted_at: "2026-03-22T22:03:00Z",
      }),
    ];

    expect(getActiveSessionTurn(turns)?.id).toBe(2);
  });

  it("prefers the active turn timer over the session timer", () => {
    expect(
      getRuntimeElapsedLabel(
        makeSession(),
        [makeTurn()],
        Date.parse("2026-03-22T22:04:30Z"),
      ),
    ).toBe("Turn 00:45");
  });

  it("falls back to the session timer when there is no active turn", () => {
    expect(
      getRuntimeElapsedLabel(
        makeSession(),
        [makeTurn({ state: "durable", durable_at: "2026-03-22T22:04:00Z" })],
        Date.parse("2026-03-22T22:04:30Z"),
      ),
    ).toBe("Session 04:30");
  });
});
