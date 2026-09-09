import { describe, expect, it } from "vitest";

import {
  advanceProviderEvidenceTransition,
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
