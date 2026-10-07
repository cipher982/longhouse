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
import type { ActivityEvidence } from "./activityEvidence";

/** What an expired work claim reads as. Never idle, never ended: unknown. */
export const ACTIVITY_UNCERTAIN_LABEL = "Activity uncertain";

/** The served attention axis (`presentation.signal.state`). */
export type SessionSignal = "attention" | "working" | "quiet" | "unknown" | "closed";

type StatusFacts = Pick<SessionStateFacts, "presentation">;

type ServedSignal = NonNullable<SessionStateFacts["presentation"]["signal"]>;

function signalWindowPassed(signal: ServedSignal, nowMs: number): boolean {
  // No window is not an expired window: a question or an idle session carries
  // no clock, and inventing one would hide it.
  if (!signal.valid_until) return false;
  const expiresAtMs = Date.parse(signal.valid_until);
  // Exclusive, as the server and catalogd treat `valid_until`.
  return !Number.isNaN(expiresAtMs) && nowMs >= expiresAtMs;
}

/**
 * The freshness gate: has the served claim outlived its window on this clock?
 * A snapshot without the signal (a cached frame from before the field existed)
 * carries no valid claim, so it reads as expired, matching `servedSignal`'s unknown.
 */
export function workClaimExpired(facts: StatusFacts, nowMs: number): boolean {
  const signal = facts.presentation.signal;
  if (!signal) return true;
  return (signal.state === "working" || signal.state === "attention") && signalWindowPassed(signal, nowMs);
}


/**
 * The server's attention axis, with the one thing the client decides itself:
 * a claim whose `valid_until` has passed reads as unknown until a new frame
 * lands. A snapshot without the field is unknown, never quiet.
 */
export function servedSignal(facts: StatusFacts, nowMs: number): SessionSignal {
  const signal = facts.presentation.signal;
  if (!signal) return "unknown";
  return signalWindowPassed(signal, nowMs) ? "unknown" : signal.state;
}

/** Is the session working, on evidence that is still valid? */
export function sessionIsWorking(facts: StatusFacts, nowMs: number): boolean {
  return servedSignal(facts, nowMs) === "working";
}

/** Does the current Runtime Host presentation carry a broad attention signal? */
export function sessionNeedsInteraction(facts: StatusFacts, nowMs: number): boolean {
  return servedSignal(facts, nowMs) === "attention";
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
