import { describe, expect, it } from "vitest";
import type { AgentSession } from "../../../services/api/agents";
import { makeSessionStateFacts } from "../../../test/sessionState";

import {
  advanceProviderEvidenceTransition,
  buildSessionLedgerState,
  providerEvidenceIdentity,
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
  it("does not imply work or a connection fault during startup grace", () => {
    const state = buildSessionLedgerState(
      session("open-work", {
        activity: "executing",
        terminalAttached: true,
        observedAt: "2026-09-09T19:00:00.000Z",
      }),
      interaction,
      0,
      false,
      null,
      true,
    );

    expect(state.connection).toBe("checking");
    expect(state.tone).toBe("quiet");
    expect(state.animateWork).toBe(false);
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
