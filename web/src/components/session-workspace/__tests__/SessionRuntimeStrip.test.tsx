import { describe, expect, it } from "vitest";
import type { AgentSession } from "../../../services/api/agents";
import { makeSessionStateFacts } from "../../../test/sessionState";

import {
  advanceProviderEvidenceTransition,
  buildSessionLedgerState,
  providerEvidenceIdentity,
  withObservationAge,
} from "../SessionRuntimeStrip";

type Transition = Parameters<typeof advanceProviderEvidenceTransition>[1];

function evidence(observedAt = "2026-09-09T19:00:00.000Z") {
  return providerEvidenceIdentity({
    observed_at: observedAt,
    state: "executing",
    tool: "Read",
    source: "provider",
  });
}

function working(overrides: Partial<Transition> = {}): Transition {
  return {
    sessionId: "session-1",
    tone: "working",
    resultAt: null,
    streamConnected: true,
    providerEvidenceIdentity: evidence(),
    preInterruptionEvidenceIdentity: null,
    hadObservedWork: true,
    interrupted: false,
    ...overrides,
  };
}

function session(
  id: string,
  options: Parameters<typeof makeSessionStateFacts>[0] = {},
): AgentSession {
  return {
    id,
    session_state: makeSessionStateFacts(options),
    runtime_display: {} as AgentSession["runtime_display"],
  } as AgentSession;
}

const interaction = {
  mode: "managed_local" as const,
  isManagedLocalSession: true,
  capabilityLabel: "Live control",
};

describe("SessionRuntimeStrip connection presentation", () => {
  it("keeps the work claim during startup grace and reports the socket separately", () => {
    const state = buildSessionLedgerState(
      session("open-work", {
        activity: "executing",
        terminalAttached: true,
        observedAt: "2026-09-09T19:00:00.000Z",
        activityValidUntil: "2026-09-09T20:00:00.000Z",
      }),
      interaction,
      0,
      false,
      null,
      true,
    );

    // The grace is about the viewer's socket, never about the provider: a live
    // window keeps its claim, the connection line says "checking", and nothing
    // animates while no frame is arriving.
    expect(state.connection).toBe("checking");
    expect(state.tone).toBe("working");
    expect(state.animateWork).toBe(false);
  });

  it("keeps a still-valid work claim when the viewer is disconnected", () => {
    const state = buildSessionLedgerState(
      session("open-work", {
        activity: "executing",
        terminalAttached: true,
        observedAt: "2026-09-09T19:00:00.000Z",
        activityValidUntil: "2026-09-09T20:00:00.000Z",
      }),
      interaction,
      0,
      false,
    );

    expect(state.tone).toBe("working");
    expect(state.connection).toBe("reconnecting");
    expect(state.animateWork).toBe(false);
  });

  it("demotes a work claim whose window has passed", () => {
    const state = buildSessionLedgerState(
      session("open-work", {
        activity: "executing",
        terminalAttached: true,
        observedAt: "2026-09-09T19:00:00.000Z",
        activityValidUntil: "2026-09-09T19:30:00.000Z",
      }),
      interaction,
      Date.parse("2026-09-09T19:45:00.000Z"),
      false,
    );

    expect(state.tone).toBe("unknown");
    expect(state.headline).toBe("Activity uncertain");
  });

  it("demotes an expired stalled claim instead of presenting it as current", () => {
    const state = buildSessionLedgerState(
      session("open-stalled", {
        activity: "stalled",
        terminalAttached: true,
        observedAt: "2026-09-09T19:00:00.000Z",
        activityValidUntil: "2026-09-09T19:30:00.000Z",
      }),
      interaction,
      Date.parse("2026-09-09T19:45:00.000Z"),
      false,
    );

    expect(state.tone).toBe("unknown");
  });

  it("retracts a work claim for a host we observed offline", () => {
    const state = buildSessionLedgerState(
      session("open-host", {
        activity: "executing",
        terminalAttached: true,
        observedAt: "2026-09-09T19:00:00.000Z",
        activityValidUntil: "2026-09-09T20:00:00.000Z",
        hostState: "offline",
      }),
      interaction,
      0,
      true,
    );

    expect(state.tone).toBe("unknown");
    expect(state.detail).toBe(
      "The host is offline; the agent may still be running.",
    );
  });

  it("leaves an idle session idle when only the host lease is stale", () => {
    const state = buildSessionLedgerState(
      session("open-host", {
        activity: "quiescent",
        terminalAttached: true,
        hostState: "stale",
      }),
      interaction,
      0,
      true,
    );

    // The host axis may retract a work claim; it may never invent an alarm
    // about a session that is simply idle.
    expect(state.tone).toBe("quiet");
  });

  it("keeps an open disconnected idle session as an exception, not a recording", () => {
    const state = buildSessionLedgerState(
      session("open-idle", {
        activity: "quiescent",
        terminalAttached: true,
      }),
      interaction,
      0,
      false,
    );

    expect(state.connection).toBe("reconnecting");
    expect(state.tone).toBe("quiet");
    expect(
      state.facts.some(
        (fact) => fact.label === "Transport" && fact.value === "reconnecting",
      ),
    ).toBe(true);
  });

  it("keeps an open history-set session recorded when no terminal is attached", () => {
    const state = buildSessionLedgerState(
      session("open-history", { activity: "quiescent" }),
      interaction,
      0,
      false,
    );

    expect(state.connection).toBe("recorded");
    expect(state.tone).toBe("quiet");
    expect(state.heartbeatAgeMs).toBeNull();
  });

  it("keeps closed history recorded and free of live receipt input", () => {
    const state = buildSessionLedgerState(
      session("closed", { closed: true }),
      interaction,
      0,
      false,
    );

    expect(state.connection).toBe("recorded");
    expect(state.heartbeatAgeMs).toBeNull();
    expect(state.receiptMarks).toHaveLength(0);
  });

  it("removes the running clock when activity expires and freezes a confirmed turn duration", () => {
    const started = Date.parse("2026-09-09T19:00:00.000Z");
    const current = session("activity-clock", {
      activity: "executing",
      terminalAttached: true,
      observedAt: new Date(started).toISOString(),
    });
    current.session_state.activity.valid_until = new Date(
      started + 10_000,
    ).toISOString();
    expect(
      buildSessionLedgerState(current, interaction, started + 5_000, true)
        .elapsedSeconds,
    ).toBe(5);
    expect(
      buildSessionLedgerState(current, interaction, started + 11_000, true)
        .elapsedSeconds,
    ).toBeNull();

    current.session_state.activity.state = "quiescent";
    expect(
      buildSessionLedgerState(current, interaction, started + 12_000, true)
        .elapsedSeconds,
    ).toBeNull();
    current.session_state.last_result_at = new Date(
      started + 12_000,
    ).toISOString();
    current.last_turn = {
      duration_ms: 12_000,
      ended_at: current.session_state.last_result_at,
    };
    expect(
      buildSessionLedgerState(current, interaction, started + 30_000, true)
        .elapsedSeconds,
    ).toBe(12);
    expect(
      buildSessionLedgerState(current, interaction, started + 60_000, true)
        .elapsedSeconds,
    ).toBe(12);
  });

  it("shows only a fresh literal command's first line, not an invented command from status prose", () => {
    const started = Date.parse("2026-09-09T19:00:00.000Z");
    const current = session("literal-command", {
      activity: "executing",
      terminalAttached: true,
      observedAt: new Date(started).toISOString(),
    });
    current.transcript_preview = {
      event_id: 1,
      text: "",
      tool_name: "Bash",
      tool_input_json: { command: "  make test\nmake validate" },
      event_origin: "tool",
      is_provisional: true,
      is_complete: false,
      is_stale: false,
    };
    const fresh = buildSessionLedgerState(
      current,
      interaction,
      started + 1000,
      true,
    );
    expect(fresh.detail).toBe("$ make test");
    expect(fresh.detailKind).toBe("literal");
    current.transcript_preview.is_stale = true;
    expect(
      buildSessionLedgerState(current, interaction, started + 1000, true)
        .detailKind,
    ).toBe("explanation");
  });
});

describe("SessionRuntimeStrip provider recovery notices", () => {
  it("does not announce initial, heartbeat-only, or stale reconnect states", () => {
    const initial = advanceProviderEvidenceTransition(null, working(), null);
    expect(initial.noticeAction).toBe("clear");
    expect(initial.notice).toBeNull();

    const interrupted = advanceProviderEvidenceTransition(
      initial.snapshot,
      working({ streamConnected: false }),
      null,
    );
    expect(interrupted.noticeAction).toBe("clear");
    expect(interrupted.notice).toBeNull();

    const heartbeatReconnect = advanceProviderEvidenceTransition(
      interrupted.snapshot,
      working(),
      null,
    );
    expect(heartbeatReconnect.noticeAction).toBe("clear");
    expect(heartbeatReconnect.notice).toBeNull();
  });

  it("announces only a refreshed provider observation after interruption", () => {
    const initial = advanceProviderEvidenceTransition(null, working(), null);
    const interrupted = advanceProviderEvidenceTransition(
      initial.snapshot,
      working({ streamConnected: false }),
      null,
    );
    const recovered = advanceProviderEvidenceTransition(
      interrupted.snapshot,
      working({
        providerEvidenceIdentity: evidence("2026-09-09T19:00:03.000Z"),
      }),
      null,
    );
    expect(recovered.noticeAction).toBe("show");
    expect(recovered.notice).not.toBeNull();
  });

  it("does not treat the first observed work as recovery", () => {
    const initialUnknown = advanceProviderEvidenceTransition(
      null,
      working({
        tone: "unknown",
        hadObservedWork: false,
        providerEvidenceIdentity: null,
      }),
      null,
    );
    const firstWork = advanceProviderEvidenceTransition(
      initialUnknown.snapshot,
      working({
        providerEvidenceIdentity: evidence("2026-09-09T19:00:01.000Z"),
      }),
      null,
    );
    expect(firstWork.noticeAction).toBe("clear");
    expect(firstWork.notice).toBeNull();
  });

  it("owns completion, new-work clearing, and routine-update retention", () => {
    const started = advanceProviderEvidenceTransition(null, working(), null);
    const completed = advanceProviderEvidenceTransition(
      started.snapshot,
      working({
        tone: "quiet",
        resultAt: "2026-09-09T19:00:04.000Z",
      }),
      "success",
    );
    expect(completed.noticeAction).toBe("show");
    expect(completed.notice).not.toBeNull();

    const newWork = advanceProviderEvidenceTransition(
      completed.snapshot,
      working({
        resultAt: completed.snapshot.resultAt,
        providerEvidenceIdentity: evidence("2026-09-09T19:00:05.000Z"),
      }),
      null,
    );
    expect(newWork.noticeAction).toBe("clear");
    expect(newWork.notice).toBeNull();

    const routineUpdate = advanceProviderEvidenceTransition(
      newWork.snapshot,
      working({
        resultAt: newWork.snapshot.resultAt,
        providerEvidenceIdentity: evidence("2026-09-09T19:00:06.000Z"),
      }),
      null,
    );
    expect(routineUpdate.noticeAction).toBe("retain");
    expect(routineUpdate.notice).toBeNull();
  });

  it("clears approval and disconnect transitions without announcing recovery", () => {
    const started = advanceProviderEvidenceTransition(null, working(), null);
    const approval = advanceProviderEvidenceTransition(
      started.snapshot,
      working({ tone: "attention" }),
      null,
    );
    expect(approval.noticeAction).toBe("clear");
    expect(approval.notice).toBeNull();

    const disconnected = advanceProviderEvidenceTransition(
      approval.snapshot,
      working({ tone: "attention", streamConnected: false }),
      null,
    );
    expect(disconnected.noticeAction).toBe("clear");
    expect(disconnected.notice).toBeNull();
  });

  it("clears a notice when the viewed session changes", () => {
    const first = advanceProviderEvidenceTransition(null, working(), null);
    const switched = advanceProviderEvidenceTransition(
      first.snapshot,
      working({ sessionId: "session-2" }),
      null,
    );
    expect(switched.noticeAction).toBe("clear");
    expect(switched.notice).toBeNull();
  });
});

describe("withObservationAge", () => {
  const nowMs = Date.parse("2026-09-10T00:00:00.000Z");
  const stale = (observedAt: string | null) => ({
    key: "no_recent_activity",
    observed_at: observedAt,
  });

  it("puts the age of a stale observation on the headline", () => {
    expect(
      withObservationAge(
        "Last observed idle",
        stale("2026-09-09T21:00:00.000Z"),
        nowMs,
      ),
    ).toBe("Last observed idle \u00B7 3h ago");
  });

  it("uses minutes and days at the right scales", () => {
    expect(
      withObservationAge(
        "Last observed idle",
        stale("2026-09-09T23:48:00.000Z"),
        nowMs,
      ),
    ).toBe("Last observed idle \u00B7 12m ago");
    expect(
      withObservationAge(
        "Last observed idle",
        stale("2026-09-07T00:00:00.000Z"),
        nowMs,
      ),
    ).toBe("Last observed idle \u00B7 3d ago");
    expect(
      withObservationAge(
        "Last observed idle",
        stale("2026-09-09T23:59:40.000Z"),
        nowMs,
      ),
    ).toBe("Last observed idle \u00B7 just now");
  });

  it("leaves every other headline alone", () => {
    expect(
      withObservationAge(
        "Using Bash",
        { key: "executing", observed_at: "2026-09-09T21:00:00.000Z" },
        nowMs,
      ),
    ).toBe("Using Bash");
    expect(withObservationAge("Idle", null, nowMs)).toBe("Idle");
  });

  it("does not invent an age it cannot compute", () => {
    expect(withObservationAge("Last observed idle", stale(null), nowMs)).toBe(
      "Last observed idle",
    );
    expect(
      withObservationAge("Last observed idle", stale("not a date"), nowMs),
    ).toBe("Last observed idle");
    // A clock skewed into the future is not a negative age.
    expect(
      withObservationAge(
        "Last observed idle",
        stale("2026-09-10T00:05:00.000Z"),
        nowMs,
      ),
    ).toBe("Last observed idle");
  });
});
