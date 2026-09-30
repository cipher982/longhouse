import { describe, expect, it } from "vitest";
import { makeSessionStateFacts } from "@/shared/test/sessionState";
import {
  delegatedWorkLabel,
  executingToolName,
  pendingInteractionLabel,
  sessionIsWorking,
  sessionNeedsInteraction,
  workClaimExpired,
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

  it("keeps delegated work live on its own fresh claim after parent activity expires", () => {
    const facts = makeSessionStateFacts({
      activity: "quiescent",
      activityValidUntil: iso(-1),
    });
    facts.presentation.primary = {
      key: "delegated_work",
      label: "Waiting on 1 background agent",
      tone: "active",
    };
    facts.delegation = {
      state: "pending",
      count: 1,
      kinds: { subagent: 1 },
      valid_until: iso(60_000),
    } as never;
    expect(sessionIsWorking(facts, NOW)).toBe(true);
    expect(delegatedWorkLabel(facts, NOW)).not.toBeNull();
    expect(workClaimExpired(facts, NOW)).toBe(false);
  });

  it("retires delegated work when its independent claim expires", () => {
    const facts = makeSessionStateFacts({ activity: "quiescent", activityValidUntil: iso(60_000) });
    facts.presentation.primary = {
      key: "delegated_work",
      label: "Waiting on 1 background agent",
      tone: "active",
    };
    facts.delegation = {
      state: "pending",
      count: 1,
      kinds: { subagent: 1 },
      valid_until: iso(-1),
    } as never;
    expect(sessionIsWorking(facts, NOW)).toBe(false);
    expect(delegatedWorkLabel(facts, NOW)).toBeNull();
    expect(workClaimExpired(facts, NOW)).toBe(true);
  });

  it("does not let delegation mask expired parent work or a keyed interaction", () => {
    const expiredParent = makeSessionStateFacts({ activity: "executing", activityValidUntil: iso(-1) });
    expiredParent.presentation.primary = {
      key: "delegated_work",
      label: "Waiting on 1 background agent",
      tone: "active",
    };
    expiredParent.delegation = {
      state: "pending",
      count: 1,
      kinds: { subagent: 1 },
      valid_until: iso(60_000),
    } as never;
    expect(sessionIsWorking(expiredParent, NOW)).toBe(false);
    expect(workClaimExpired(expiredParent, NOW)).toBe(true);

    const interaction = makeSessionStateFacts({
      activity: "executing",
      activityValidUntil: iso(-1),
      pendingInteraction: true,
    });
    expect(sessionNeedsInteraction(interaction, NOW)).toBe(true);
    expect(workClaimExpired(interaction, NOW)).toBe(false);
  });

  it("counts a served live tone as working while the loop itself is quiescent", () => {
    const facts = makeSessionStateFacts({ activity: "quiescent" });
    facts.presentation.primary = {
      key: "delegated_work",
      label: "Waiting on 1 background agent",
      tone: "active",
    };
    facts.delegation = {
      state: "pending",
      count: 1,
      kinds: { subagent: 1 },
      valid_until: iso(60_000),
    } as never;
    expect(sessionIsWorking(facts, NOW)).toBe(true);
    expect(delegatedWorkLabel(facts, NOW)).not.toBeNull();
  });

  it("never calls a closed session working", () => {
    expect(sessionIsWorking(makeSessionStateFacts({ activity: "executing", closed: true }), NOW)).toBe(false);
  });


  it("names a tool only while one is executing", () => {
    expect(executingToolName({ state: "executing", tool: " Bash " })).toBe("Bash");
    expect(executingToolName({ state: "thinking", tool: "Bash" })).toBeNull();
    expect(executingToolName(null)).toBeNull();
  });
});
