/**
 * The one client reading of the server's status vocabulary.
 *
 * The server authors every status word (`session_state_contract._primary`:
 * "Thinking", "Using Bash", "Needs answer", "Waiting on 1 background agent",
 * ...). A client shows `presentation.primary.label` verbatim and adds only a
 * duration. The one thing the client decides for itself is freshness: a work
 * claim whose `valid_until` has passed on the reader's clock is no longer
 * allowed to speak, so a cached snapshot can never keep saying "Using Bash".
 *
 * The session header, the composer, the runtime strip, the Hearth and the
 * live-status lab all read status through these functions.
 */
import type { SessionStateFacts } from "@/shared/api/agents";
import {
  activityClaimIsStale,
  delegationEvidenceIsLive,
  type ActivityEvidence,
} from "./activityEvidence";

/** What an expired work claim reads as. Never idle, never ended: unknown. */
export const ACTIVITY_UNCERTAIN_LABEL = "Activity uncertain";

type StatusFacts = Pick<
  SessionStateFacts,
  "activity" | "presentation" | "disposition" | "pending_interaction" | "delegation"
>;

function delegatedWorkIsLive(facts: Pick<SessionStateFacts, "delegation">, nowMs: number): boolean {
  const delegation = facts.delegation;
  return Boolean(
    delegation &&
      delegation.state === "pending" &&
      delegation.count > 0 &&
      delegationEvidenceIsLive(delegation, nowMs),
  );
}

function isKeyedInteraction(facts: Pick<SessionStateFacts, "presentation" | "pending_interaction">): boolean {
  const key = facts.presentation.primary?.key;
  return facts.pending_interaction != null || key === "needs_answer" || key === "needs_approval";
}

/** The freshness gate: has the served work claim outlived its window? */
export function workClaimExpired(
  facts: StatusFacts,
  nowMs: number,
): boolean {
  // A question/approval is its own served claim. It must not disappear just
  // because the activity head that preceded it aged out.
  if (isKeyedInteraction(facts)) return false;

  // Do not let a fresh delegation hide an expired thinking/executing claim.
  // The server only mints delegated_work once the parent loop is quiescent,
  // but keeping this fence here protects mixed/older snapshots too.
  if (
    (facts.activity.state === "thinking" || facts.activity.state === "executing") &&
    activityClaimIsStale(facts.activity, nowMs)
  ) {
    return true;
  }

  if (facts.presentation.primary?.key === "delegated_work") {
    // A delegated headline is valid on the delegation axis, not the parent's
    // per-tool activity window. Missing delegation evidence cannot keep a
    // cached delegated-work claim alive.
    return !delegatedWorkIsLive(facts, nowMs);
  }
  return activityClaimIsStale(facts.activity, nowMs);
}

/**
 * Is the session working, on evidence that is still valid?
 *
 * The served tone is read as well as `activity.state`: a delegated-work or
 * starting session is live (`active`) while its own loop is quiescent.
 */
export function sessionIsWorking(facts: StatusFacts, nowMs: number): boolean {
  if (facts.disposition.state === "closed") return false;
  if (isKeyedInteraction(facts)) return false;
  if (workClaimExpired(facts, nowMs)) return false;
  const tone = facts.presentation.primary?.tone ?? null;
  return (
    tone === "running" ||
    tone === "thinking" ||
    tone === "active" ||
    facts.activity.state === "thinking" ||
    facts.activity.state === "executing"
  );
}

/** Is the session waiting on the user (a question, an approval, a stall)? */
export function sessionNeedsInteraction(facts: StatusFacts, nowMs: number): boolean {
  if (facts.disposition.state === "closed") return false;
  if (isKeyedInteraction(facts)) return true;
  if (workClaimExpired(facts, nowMs)) return false;
  const tone = facts.presentation.primary?.tone ?? null;
  return tone === "blocked" || tone === "stalled";
}

/**
 * The delegated-work sentence ("Waiting on 1 background agent"), or null.
 * The tool field belongs to the main loop, so the client must never re-derive
 * this from `activity`; the delegation claim has its own freshness window.
 */
export function delegatedWorkLabel(facts: StatusFacts, nowMs: number): string | null {
  const primary = facts.presentation.primary;
  return primary?.key === "delegated_work" &&
    primary.label &&
    !workClaimExpired(facts, nowMs)
    ? primary.label
    : null;
}

/**
 * The words for a pending interaction: always the server's own copy. It tells
 * a question ("Needs answer") from an approval ("Needs approval"); a client
 * that invents "Waiting for approval" makes a question read as something else.
 */
export function pendingInteractionLabel(facts: Pick<SessionStateFacts, "presentation">): string {
  return facts.presentation.primary?.label?.trim() || "Needs attention";
}

/** The label for a working session: the served label, verbatim. */
export function workingStatusLabel(facts: Pick<SessionStateFacts, "presentation">): string {
  return facts.presentation.primary?.label?.trim() || "Working";
}

/**
 * The tool a session is running right now, or null. A finished tool leaves
 * its name on the activity fact, so only `executing` may claim one.
 */
export function executingToolName(activity: (ActivityEvidence & { tool?: string | null }) | null | undefined): string | null {
  if (activity?.state !== "executing") return null;
  return activity.tool?.trim() || null;
}
