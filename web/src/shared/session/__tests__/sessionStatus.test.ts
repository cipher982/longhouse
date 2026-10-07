import { describe, expect, it } from "vitest";
import { makeSessionStateFacts, mirrorServedSignal } from "@/shared/test/sessionState";
import type { SessionStateFacts } from "@/shared/api/agents";

/** Re-serve the signal after a test hand-edits the axes, as the server would. */
function reserve(facts: SessionStateFacts): SessionStateFacts {
  facts.presentation.signal = mirrorServedSignal(facts.presentation.primary, {
    activity: facts.activity,
    delegation: facts.delegation,
  });
  return facts;
}
import {
  delegatedWorkLabel,
  executingToolName,
  pendingInteractionLabel,
  sessionIsWorking,
  sessionNeedsInteraction,
  servedSignal,
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

  it("reads a snapshot without the served signal as unknown on both gates", () => {
    const facts = makeSessionStateFacts({ activity: "executing", activityValidUntil: iso(60_000) });
    delete (facts.presentation as { signal?: unknown }).signal;
    expect(servedSignal(facts, NOW)).toBe("unknown");
    expect(workClaimExpired(facts, NOW)).toBe(true);
    expect(sessionIsWorking(facts, NOW)).toBe(false);
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
    reserve(facts);
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
      valid_until: iso(0),
    } as never;
    reserve(facts);
    expect(sessionIsWorking(facts, NOW)).toBe(false);
    expect(delegatedWorkLabel(facts, NOW)).toBeNull();
    expect(workClaimExpired(facts, NOW)).toBe(true);
  });

  it("does not let an expired parent claim retire an answerable keyed interaction", () => {
    // The server only serves delegated work once the parent loop is quiescent,
    // so the old client fence against "delegated over expired parent" has no
    // input left to guard; what remains is that a question never expires.
    const interaction = makeSessionStateFacts({
      activity: "executing",
      activityValidUntil: iso(-1),
      pendingInteraction: true,
    });
    expect(sessionNeedsInteraction(interaction, NOW)).toBe(true);
    expect(workClaimExpired(interaction, NOW)).toBe(false);
  });

  it("does not keep an unanswerable stale interaction on the attention axis", () => {
    const unanswerable = makeSessionStateFacts({
      activity: "quiescent",
      pendingInteraction: true,
    });
    unanswerable.pending_interaction!.can_respond = false;
    unanswerable.presentation.primary = { key: "idle", label: "Idle", tone: "idle" };
    reserve(unanswerable);
    expect(servedSignal(unanswerable, NOW)).toBe("quiet");
    expect(sessionNeedsInteraction(unanswerable, NOW)).toBe(false);
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
    reserve(facts);
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
