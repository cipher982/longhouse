import type { AgentSession } from "../../services/api/agents";

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
 * `getRunningTurnStartMs` in `components/instruments/toolActivity.ts`) —
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
  // The route's own tone (`session-workspace-route--tone-<tone>`) already
  // reads `presentation.primary.tone` as the authoritative live/attention
  // signal — a session can be "blocked" or "stalled" with activity.state
  // still "quiescent" underneath, so activity.state alone under-detects.
  const primaryTone = facts.presentation.primary?.tone ?? null;
  const closed = facts.disposition.state === "closed";
  const pending =
    !closed &&
    (facts.pending_interaction != null ||
      primaryTone === "blocked" ||
      primaryTone === "stalled");
  const working =
    !closed &&
    (primaryTone === "running" ||
      primaryTone === "thinking" ||
      primaryTone === "active" ||
      facts.activity.state === "thinking" ||
      facts.activity.state === "executing");

  if (pending) {
    // A real provider question always reads as "Waiting for approval";
    // a blocked/stalled tone without one (e.g. "No progress for 31m")
    // uses the server's own label so the wording never disagrees with
    // the runtime strip right below it.
    const label =
      facts.pending_interaction != null
        ? "Waiting for approval"
        : facts.presentation.primary?.label?.trim() || "Needs attention";
    return { tone: "attention", text: label };
  }

  if (working) {
    const tool = facts.activity.tool?.trim();
    const fallbackAnchorMs = Date.parse(facts.activity.observed_at ?? "");
    const anchorMs = turnStartMs ?? (Number.isFinite(fallbackAnchorMs) ? fallbackAnchorMs : null);
    const elapsedSeconds = anchorMs != null
      ? Math.max(0, Math.floor((nowMs - anchorMs) / 1_000))
      : null;
    const using = tool ? `Using ${tool}` : "Working";
    return {
      tone: "live",
      text:
        elapsedSeconds != null
          ? `${using} for ${formatDurationWords(elapsedSeconds)}`
          : using,
    };
  }

  if (!closed && facts.activity.state === "unknown") {
    return { tone: "unknown", text: "Activity uncertain" };
  }

  const lastMs = Date.parse(
    facts.last_result_at ?? facts.activity.observed_at ?? "",
  );
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
 * The header's identity line as one sentence — "OMP working in zerg on
 * cinder, 57 messages and 334 tool calls so far" — instead of a dot-joined
 * fragment list plus a separate "N messages · N tool calls loaded" pill.
 * "working" only applies while the header tone is live; an ended/idle
 * session reads "OMP in zerg on cinder" instead of claiming it's still
 * working.
 */
export function buildSessionMetaSentence({
  provider,
  project,
  host,
  messages,
  toolCalls,
  tone,
}: {
  provider: string | null;
  project: string | null;
  host: string | null;
  messages: number;
  toolCalls: number;
  tone: SessionHeaderStateTone;
}): string | null {
  const parts: string[] = [];
  if (provider) parts.push(provider);
  if (tone === "live") parts.push("working");
  if (project) parts.push(`in ${project}`);
  if (host) parts.push(`on ${host}`);
  let sentence = parts.length > 1 ? parts.join(" ") : provider ? provider : null;

  const counts: string[] = [];
  if (messages > 0) counts.push(plural(messages, "message"));
  if (toolCalls > 0) counts.push(plural(toolCalls, "tool call"));
  const countsText = counts.length > 0 ? `${counts.join(" and ")} so far` : null;

  if (!sentence) return countsText;
  return countsText ? `${sentence}, ${countsText}` : sentence;
}

export interface SessionMetaSentenceParts {
  /** Text up to (not including) the tool-call count — already carries the
   * right leading punctuation/conjunction. */
  before: string;
  toolCalls: number;
  toolCallsWord: string;
  /** Always " so far" — there is a count to trail once toolCalls > 0. */
  after: string;
}

/**
 * Phase 4 (Instruments, web-restyle-signal.md): "57 messages" stays plain
 * text but "334 tool calls" renders as a live Nixie. Splits
 * buildSessionMetaSentence's output around the tool-call count so the page
 * can wrap just that number, rather than re-deriving the sentence text
 * twice. Returns null when there are no tool calls to highlight — the
 * caller falls back to the plain buildSessionMetaSentence() string.
 */
export function buildSessionMetaSentenceParts({
  provider,
  project,
  host,
  messages,
  toolCalls,
  tone,
}: {
  provider: string | null;
  project: string | null;
  host: string | null;
  messages: number;
  toolCalls: number;
  tone: SessionHeaderStateTone;
}): SessionMetaSentenceParts | null {
  if (toolCalls <= 0) return null;

  const parts: string[] = [];
  if (provider) parts.push(provider);
  if (tone === "live") parts.push("working");
  if (project) parts.push(`in ${project}`);
  if (host) parts.push(`on ${host}`);
  const sentence = parts.length > 1 ? parts.join(" ") : provider ? provider : null;

  let before = sentence ?? "";
  if (messages > 0) {
    before += `${sentence ? ", " : ""}${plural(messages, "message")} and `;
  } else {
    before += sentence ? ", " : "";
  }

  return {
    before,
    toolCalls,
    toolCallsWord: toolCalls === 1 ? "tool call" : "tool calls",
    after: " so far",
  };
}
