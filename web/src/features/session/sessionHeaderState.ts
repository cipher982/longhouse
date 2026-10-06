import type { AgentSession } from "@/shared/api/agents";
import {
  ACTIVITY_UNCERTAIN_LABEL,
  delegatedWorkLabel,
  pendingInteractionLabel,
  sessionIsWorking,
  sessionNeedsInteraction,
  workClaimExpired,
  workingStatusLabel,
} from "@/shared/session/sessionStatus";

export type SessionHeaderStateTone = "live" | "attention" | "unknown" | "cool";

export interface SessionHeaderStateInfo {
  tone: SessionHeaderStateTone;
  text: string;
}

function formatDurationWords(totalSeconds: number): string {
  if (totalSeconds < 60) return "under a minute";
  const minutes = Math.round(totalSeconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  const hourPart = `${hours} hour${hours === 1 ? "" : "s"}`;
  return remainder === 0
    ? hourPart
    : `${hourPart} ${remainder} minute${remainder === 1 ? "" : "s"}`;
}

export function formatClockTime(ms: number): string | null {
  if (!Number.isFinite(ms)) return null;
  return new Date(ms).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Composer timer readout: "35:37" (mm:ss), "1:05:12" past an hour. */
export function formatElapsedClock(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(s / 3_600);
  const minutes = Math.floor((s % 3_600) / 60);
  const seconds = s % 60;
  const ss = String(seconds).padStart(2, "0");
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${ss}`;
  }
  return `${minutes}:${ss}`;
}

/**
 * The header's right-side state: a dot plus one sentence. Four shapes only —
 * live (breathing ember), attention (a provider question is pending),
 * unknown (the provider may still be active but evidence is stale), and cool
 * (idle or ended) — because that is all the header has room to say at a
 * glance; the full evidence disclosure still lives in the runtime strip below.
 *
 * `turnStartMs` is the one shared turn-elapsed anchor (see
 * `getRunningTurnStartMs` in `shared/instruments/toolActivity.ts`) —
 * pass it whenever the caller has the loaded thread, so this sentence's
 * "for N minutes" agrees with the composer clock and the readout rail's
 * Turn readout. Omitting it falls back to the activity heartbeat, which is
 * "how long since the last observed tool signal", not "how long has this
 * turn run" — the three-clocks-disagree bug this parameter exists to avoid.
 */
export function getSessionHeaderState(
  session: Pick<AgentSession, "session_state">,
  nowMs: number,
  turnStartMs?: number | null,
): SessionHeaderStateInfo {
  const facts = session.session_state;
  const primaryTone = facts.presentation.primary?.tone ?? null;
  const primaryKey = facts.presentation.primary?.key ?? null;
  const closed = facts.disposition.state === "closed";
  // A served tone is a verdict about the moment it was minted. The reader's
  // clock decides when that ended, so an expired window may not keep the
  // header saying "Using Bash for 50 minutes".
  const staleClaim = workClaimExpired(facts, nowMs);

  if (sessionNeedsInteraction(facts, nowMs)) {
    return { tone: "attention", text: pendingInteractionLabel(facts) };
  }

  if (sessionIsWorking(facts, nowMs)) {
    // Delegated work names the work, not the elapsed turn.
    const delegated = delegatedWorkLabel(facts, nowMs);
    if (delegated) return { tone: "live", text: delegated };
    const fallbackAnchorMs = Date.parse(facts.activity.observed_at ?? "");
    const anchorMs = turnStartMs ?? (Number.isFinite(fallbackAnchorMs) ? fallbackAnchorMs : null);
    const elapsedSeconds = anchorMs != null
      ? Math.max(0, Math.floor((nowMs - anchorMs) / 1_000))
      : null;
    // The server's label, verbatim; the client adds only the duration.
    const label = workingStatusLabel(facts);
    return {
      tone: "live",
      text:
        elapsedSeconds != null
          ? `${label} for ${formatDurationWords(elapsedSeconds)}`
          : label,
    };
  }

  // No fresh activity claim is not the same as no evidence. The server keeps
  // an at-rest verdict when other evidence settles it: a Console run that
  // ended (Console work only happens inside a run), a Console slot ready for
  // its first turn, or a Helm idle kept by a fresh control lease. Reading
  // "Activity uncertain" under those contradicts evidence the page holds.
  const servedAtRest =
    facts.activity.state === "unknown" &&
    (primaryKey === "idle" || primaryKey === "ready" || (primaryKey === "ended" && primaryTone === "closed"));
  if (!closed && !servedAtRest && (staleClaim || facts.activity.state === "unknown")) {
    return { tone: "unknown", text: ACTIVITY_UNCERTAIN_LABEL };
  }

  const lastMs = Date.parse(facts.last_result_at ?? "");
  const clock = formatClockTime(lastMs);
  if (closed) {
    return { tone: "cool", text: clock ? `Ended ${clock}` : "Ended" };
  }
  return { tone: "cool", text: clock ? `Idle since ${clock}` : "Idle" };
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/**
 * The app bar's meta line as plain items, joined with " · " by the caller:
 * "Claude · zerg · cinder · 29 msgs · 85 tools". Every value is plain text;
 * no count is styled as a control. Counts come from the session's own
 * totals when it has them, so a partly loaded transcript does not shrink
 * them, and fall back to what is loaded.
 */
export function buildSessionMetaItems({
  provider,
  project,
  host,
  messages,
  toolCalls,
}: {
  provider: string | null;
  project: string | null;
  host: string | null;
  messages: number;
  toolCalls: number;
}): string[] {
  const items: string[] = [];
  if (provider) items.push(provider);
  if (project) items.push(project);
  if (host && host !== project) items.push(host);
  if (messages > 0) items.push(plural(messages, "msg"));
  if (toolCalls > 0) items.push(plural(toolCalls, "tool"));
  return items;
}
