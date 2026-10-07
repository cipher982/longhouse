import { describe, expect, it } from "vitest";
import {
  buildSessionMetaItems,
  formatElapsedClock,
  getSessionHeaderState,
} from "../sessionHeaderState";
import type { AgentSession } from "@/shared/api/agents";
import { mirrorServedSignal } from "@/shared/test/sessionState";

function session(overrides: {
  disposition?: string;
  pendingInteraction?: unknown;
  activityState?: string;
  tool?: string | null;
  observedAt?: string | null;
  validUntil?: string | null;
  delegation?: unknown;
  primaryTone?: string | null;
  primaryLabel?: string | null;
  primaryKey?: string | null;
  lastResultAt?: string | null;
}): Pick<AgentSession, "session_state"> {
  const activity = {
    state: overrides.activityState ?? "quiescent",
    tool: overrides.tool ?? null,
    observed_at: overrides.observedAt ?? null,
    valid_until: overrides.validUntil ?? null,
  };
  const primary =
    overrides.primaryTone != null
      ? {
          key: overrides.primaryKey ?? "idle",
          tone: overrides.primaryTone,
          label: overrides.primaryLabel ?? "",
        }
      : null;
  return {
    session_state: {
      disposition: { state: overrides.disposition ?? "open" },
      pending_interaction: overrides.pendingInteraction ?? null,
      activity,
      delegation: overrides.delegation,
      presentation: {
        primary,
        signal: mirrorServedSignal(primary, {
          activity,
          delegation: overrides.delegation as { valid_until?: string | null } | null | undefined,
        }),
      },
      last_result_at: overrides.lastResultAt ?? null,
    } as never,
  };
}

describe("getSessionHeaderState", () => {
  it("reads a real provider question as attention, with the server's own copy", () => {
    const state = getSessionHeaderState(
      session({
        pendingInteraction: { id: "1" },
        primaryTone: "blocked",
        primaryLabel: "Needs answer",
      }),
      Date.now(),
    );
    expect(state).toEqual({ tone: "attention", text: "Needs answer" });
  });

  it("reads a blocked/stalled presentation tone as attention even when activity.state is quiescent", () => {
    const state = getSessionHeaderState(
      session({ primaryTone: "stalled", primaryLabel: "No progress for 31m" }),
      Date.now(),
    );
    expect(state).toEqual({ tone: "attention", text: "No progress for 31m" });
  });

  it("reads an executing session as live, with the server's label and a client duration", () => {
    const now = Date.parse("2026-04-15T16:30:00Z");
    const state = getSessionHeaderState(
      session({
        activityState: "executing",
        tool: "hub",
        observedAt: "2026-04-15T15:55:00Z",
        primaryTone: "running",
        primaryKey: "executing",
        primaryLabel: "Using hub",
      }),
      now,
    );
    expect(state.tone).toBe("live");
    expect(state.text).toBe("Using hub for 35 minutes");
  });

  it("shows whatever label the server minted, never a client rewrite", () => {
    // A Console run with no tool is served as "Working", an executing loop
    // with no tool name as "Running", a run that has not landed as "Starting".
    const now = Date.parse("2026-04-15T16:30:00Z");
    for (const [activityState, primaryKey, primaryTone, primaryLabel] of [
      ["executing", "executing", "running", "Running"],
      ["unknown", "executing", "running", "Working"],
      ["unknown", "starting", "active", "Starting"],
    ] as const) {
      const state = getSessionHeaderState(
        session({ activityState, primaryKey, primaryTone, primaryLabel, observedAt: "2026-04-15T16:28:00Z" }),
        now,
      );
      expect(state).toEqual({ tone: "live", text: `${primaryLabel} for 2 minutes` });
    }
  });

  it("uses the server's delegated label instead of a tool name", () => {
    // The main loop is idle; something it started is not. The tool field
    // belongs to the loop, so the client must not re-derive this sentence.
    const now = Date.parse("2026-04-15T16:30:00Z");
    const state = getSessionHeaderState(
      session({
        activityState: "quiescent",
        tool: "Bash",
        observedAt: "2026-04-15T16:29:00Z",
        validUntil: "2026-04-15T16:25:00Z",
        delegation: {
          state: "pending",
          count: 1,
          kinds: { subagent: 1 },
          valid_until: "2026-04-15T16:35:00Z",
        },
        primaryTone: "active",
        primaryKey: "delegated_work",
        primaryLabel: "Waiting on 1 background agent",
      }),
      now,
    );
    expect(state).toEqual({ tone: "live", text: "Waiting on 1 background agent" });
  });

  it("does not name a tool for a thinking activity that still carries one", () => {
    // A finished tool leaves its name on the activity fact, so a non-empty
    // `tool` is not evidence that one is running. Without the gate this read
    // "Using Bash for 1 minute" while the session was only thinking — the
    // defect that made an idle parent look busy on the strength of a child's
    // tool name.
    const now = Date.parse("2026-04-15T16:30:00Z");
    const state = getSessionHeaderState(
      session({
        activityState: "thinking",
        tool: "Bash",
        observedAt: "2026-04-15T16:29:00Z",
        primaryTone: "thinking",
        primaryKey: "thinking",
        primaryLabel: "Thinking",
      }),
      now,
    );
    expect(state.tone).toBe("live");
    expect(state.text).toBe("Thinking for 1 minute");
    expect(state.text).not.toContain("Bash");
  });

  it("does not turn expired activity evidence into a positive idle claim", () => {
    const now = Date.parse("2026-04-15T16:30:00Z");
    const state = getSessionHeaderState(
      session({
        activityState: "unknown",
        observedAt: "2026-04-15T16:29:00Z",
        lastResultAt: "2026-04-15T15:55:00Z",
      }),
      now,
    );
    expect(state).toEqual({ tone: "unknown", text: "Activity uncertain" });
  });

  it("reads a Console session whose run ended as idle, not uncertain", () => {
    // F6: a completed Console turn has no fresh activity claim, but the
    // ended run is itself the evidence nothing is running.
    const state = getSessionHeaderState(
      session({
        activityState: "unknown",
        primaryKey: "ended",
        primaryTone: "closed",
        primaryLabel: "Ended",
        lastResultAt: "2026-04-15T15:55:00Z",
      }),
      Date.parse("2026-04-15T16:30:00Z"),
    );
    expect(state.tone).toBe("cool");
    expect(state.text).toMatch(/^Idle since /);
  });

  it("keeps a server-kept Helm idle and a ready Console slot out of uncertain", () => {
    for (const primaryKey of ["idle", "ready"]) {
      const state = getSessionHeaderState(
        session({ activityState: "unknown", primaryKey, primaryTone: "idle", primaryLabel: "Idle" }),
        Date.now(),
      );
      expect(state).toEqual({ tone: "cool", text: "Idle" });
    }
  });

  it("still reads an activity-unknown verdict as uncertain", () => {
    const state = getSessionHeaderState(
      session({
        activityState: "unknown",
        primaryKey: "activity_unknown",
        primaryTone: "quiet",
        primaryLabel: "Activity unknown",
      }),
      Date.now(),
    );
    expect(state).toEqual({ tone: "unknown", text: "Activity uncertain" });
  });

  it("stops claiming work when the served window has passed", () => {
    const now = Date.parse("2026-04-15T16:30:00Z");
    const state = getSessionHeaderState(
      session({
        activityState: "executing",
        tool: "Bash",
        observedAt: "2026-04-15T16:12:00Z",
        validUntil: "2026-04-15T16:22:00Z",
        primaryTone: "running",
        primaryLabel: "Using Bash",
      }),
      now,
    );
    expect(state).toEqual({ tone: "unknown", text: "Activity uncertain" });
  });

  it("keeps claiming work while the served window is still valid", () => {
    const now = Date.parse("2026-04-15T16:30:00Z");
    const state = getSessionHeaderState(
      session({
        activityState: "executing",
        tool: "Bash",
        observedAt: "2026-04-15T16:29:00Z",
        validUntil: "2026-04-15T16:40:00Z",
        primaryTone: "running",
        primaryLabel: "Using Bash",
      }),
      now,
    );
    expect(state.tone).toBe("live");
  });

  it("stops presenting an expired stall as an attention state", () => {
    const now = Date.parse("2026-04-15T16:30:00Z");
    const state = getSessionHeaderState(
      session({
        activityState: "stalled",
        observedAt: "2026-04-15T16:00:00Z",
        validUntil: "2026-04-15T16:10:00Z",
        primaryTone: "stalled",
        primaryKey: "stalled",
        primaryLabel: "No progress for 31m",
      }),
      now,
    );
    expect(state).toEqual({ tone: "unknown", text: "Activity uncertain" });
  });

  it("reads a closed session as cool/ended", () => {
    const state = getSessionHeaderState(
      session({ disposition: "closed", lastResultAt: "2026-04-15T16:12:00Z" }),
      Date.now(),
    );
    expect(state.tone).toBe("cool");
    expect(state.text).toMatch(/^Ended/);
  });

  it("keeps a closed session ended when activity evidence has expired", () => {
    const now = Date.parse("2026-04-15T16:30:00Z");
    const state = getSessionHeaderState(
      session({
        disposition: "closed",
        activityState: "unknown",
        lastResultAt: "2026-04-15T16:12:00Z",
      }),
      now,
    );
    expect(state.tone).toBe("cool");
    expect(state.text).toMatch(/^Ended /);
  });

  it("reads an open, non-working session as idle", () => {
    const state = getSessionHeaderState(
      session({ lastResultAt: "2026-04-15T16:12:00Z" }),
      Date.now(),
    );
    expect(state.tone).toBe("cool");
    expect(state.text).toMatch(/^Idle since/);
  });

  it("does not invent a new idle boundary when heartbeat evidence renews", () => {
    const first = getSessionHeaderState(
      session({ observedAt: "2026-04-15T16:12:00Z" }),
      Date.parse("2026-04-15T16:12:00Z"),
    );
    const renewed = getSessionHeaderState(
      session({ observedAt: "2026-04-15T16:22:00Z" }),
      Date.parse("2026-04-15T16:22:00Z"),
    );
    expect(renewed).toEqual(first);
    expect(renewed.tone).toBe("cool");
  });
});

describe("buildSessionMetaItems", () => {
  it("lists provider, project, host and counts as plain items", () => {
    expect(
      buildSessionMetaItems({
        provider: "Claude",
        project: "zerg",
        host: "cinder",
        messages: 29,
        toolCalls: 85,
      }),
    ).toEqual(["Claude", "zerg", "cinder", "29 msgs", "85 tools"]);
  });

  it("drops missing parts and zero counts", () => {
    expect(
      buildSessionMetaItems({ provider: "OMP", project: null, host: null, messages: 0, toolCalls: 0 }),
    ).toEqual(["OMP"]);
  });

  it("uses the singular for one and does not repeat a host equal to the project", () => {
    expect(
      buildSessionMetaItems({ provider: null, project: "cinder", host: "cinder", messages: 1, toolCalls: 1 }),
    ).toEqual(["cinder", "1 msg", "1 tool"]);
  });

  it("returns nothing when nothing is known", () => {
    expect(
      buildSessionMetaItems({ provider: null, project: null, host: null, messages: 0, toolCalls: 0 }),
    ).toEqual([]);
  });
});

describe("formatElapsedClock", () => {
  it("formats minutes:seconds", () => {
    expect(formatElapsedClock(35 * 60 + 37)).toBe("35:37");
  });

  it("formats hours:minutes:seconds past an hour", () => {
    expect(formatElapsedClock(60 * 65 + 5)).toBe("1:05:05");
  });
});
