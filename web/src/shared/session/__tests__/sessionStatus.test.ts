import { describe, expect, it } from "vitest";
import { makeSessionStateFacts } from "@/shared/test/sessionState";
import {
  delegatedWorkLabel,
  executingToolName,
  pendingInteractionLabel,
  sessionIsWorking,
  sessionNeedsInteraction,
  workClaimExpired,
  workingStatusLabel,
} from "../sessionStatus";

const NOW = Date.parse("2026-09-27T12:00:00Z");
const iso = (offsetMs: number) => new Date(NOW + offsetMs).toISOString();

describe("sessionStatus", () => {
  it("treats a fresh work claim as working and an expired one as not", () => {
    const fresh = makeSessionStateFacts({ activity: "executing", activityValidUntil: iso(60_000) });
    const expired = makeSessionStateFacts({ activity: "executing", activityValidUntil: iso(-1) });
    expect(sessionIsWorking(fresh, NOW)).toBe(true);
    expect(workClaimExpired(fresh, NOW)).toBe(false);
    expect(sessionIsWorking(expired, NOW)).toBe(false);
    expect(workClaimExpired(expired, NOW)).toBe(true);
  });

  it("counts a served live tone as working while the loop itself is quiescent", () => {
    const facts = makeSessionStateFacts({ activity: "quiescent" });
    facts.presentation.primary = {
      key: "delegated_work",
      label: "Waiting on 1 background agent",
      tone: "active",
    };
    expect(sessionIsWorking(facts, NOW)).toBe(true);
    expect(delegatedWorkLabel(facts)).toBe("Waiting on 1 background agent");
    expect(workingStatusLabel(facts)).toBe("Waiting on 1 background agent");
  });

  it("never calls a closed session working", () => {
    expect(sessionIsWorking(makeSessionStateFacts({ activity: "executing", closed: true }), NOW)).toBe(false);
  });

  it("reads the working and pending wording from the server verbatim", () => {
    expect(workingStatusLabel(makeSessionStateFacts({ activity: "thinking", tool: "Bash" }))).toBe("Thinking");
    expect(workingStatusLabel(makeSessionStateFacts({ activity: "executing" }))).toBe("Using Shell");
    const pending = makeSessionStateFacts({ pendingInteraction: true });
    expect(sessionNeedsInteraction(pending, NOW)).toBe(true);
    expect(pendingInteractionLabel(pending)).toBe("Needs answer");
    expect(delegatedWorkLabel(pending)).toBeNull();
  });

  it("names a tool only while one is executing", () => {
    expect(executingToolName({ state: "executing", tool: " Bash " })).toBe("Bash");
    expect(executingToolName({ state: "thinking", tool: "Bash" })).toBeNull();
    expect(executingToolName(null)).toBeNull();
  });
});
