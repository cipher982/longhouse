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
    expect(initial.notice).toBeNull();

    const interrupted = advanceProviderEvidenceTransition(
      initial.snapshot,
      working({ streamConnected: false }),
      null,
    );
    expect(interrupted.notice).toBeNull();

    const heartbeatReconnect = advanceProviderEvidenceTransition(
      interrupted.snapshot,
      working(),
      null,
    );
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
    expect(recovered.notice).toBe("Fresh provider evidence restored.");
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
    expect(firstWork.notice).toBeNull();
  });
});
