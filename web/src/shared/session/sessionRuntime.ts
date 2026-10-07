import type {
  AgentSession,
  AgentSessionStatus,
  SessionRuntimeDisplay,
  SessionStateFacts,
} from "@/shared/api/agents";
import { isWirePresenceState, type WirePresenceState } from "@/generated/presence-states";
import { servedSignal, type SessionSignal } from "./sessionStatus";
export type KnownPresenceState = WirePresenceState;
export type RuntimeTone = "inactive" | "quiet" | "active" | "thinking" | "running" | "blocked" | "stalled" | "idle" | "closed";

type TimelineRuntimeOverlay = {
  timeline_anchor_at?: string | null;
  runtime_source?: string | null;
  status?: AgentSessionStatus | string | null;
  presence_state?: string | null;
  presence_tool?: string | null;
  presence_updated_at?: string | null;
  last_live_at?: string | null;
  display_phase?: string | null;
  active_tool?: string | null;
  confidence?: string | null;
  runtime_display: SessionRuntimeDisplay;
  capabilities?: AgentSession["capabilities"] | null;
};

export type TimelineRuntimeSession = Pick<
  AgentSession,
  "ended_at" | "last_activity_at" | "timeline_anchor_at" | "capabilities" | "runtime_display" | "session_state" | "user_state"
> &
  Partial<Omit<TimelineRuntimeOverlay, "runtime_display">>;

export function isSessionClosed(
  session: Pick<AgentSession, "session_state"> | null | undefined,
): boolean {
  return session?.session_state.disposition.state === "closed";
}

function isUserActive(session: Pick<AgentSession, "user_state">): boolean {
  return session.user_state == null || session.user_state === "active";
}

/**
 * True only for an active session whose served headline is a pending
 * interaction: an explicit question or approval ("Needs you"). Narrower than
 * the attention dot, which also lights for a stall or a failed launch.
 */
export function needsSessionAttention(
  session: Pick<AgentSession, "session_state" | "user_state">,
): boolean {
  const primaryKey = session.session_state.presentation.primary?.key;
  const hasCurrentInteraction =
    session.session_state.pending_interaction != null &&
    (primaryKey === "needs_answer" || primaryKey === "needs_approval");
  return !isSessionClosed(session) && isUserActive(session) && hasCurrentInteraction;
}

/**
 * The single attention axis for a timeline row, served by the Runtime Host
 * (`presentation.signal`) and drawn the same way on iOS and the menu bar:
 *   - attention: WAITING ON YOU — steady amber, never pulses.
 *   - working:   actively running — teal, pulses (live only).
 *   - quiet:     idle — grey, static.
 *   - unknown:   no current evidence — grey, static.
 *   - closed:    ended — dimmed, static.
 * Drives `data-signal` on the row; CSS owns the colors. The client adds only
 * what the server cannot know: its own clock, a muted session, and a global
 * connectivity banner that owns severity.
 */
export type TimelineSignal = SessionSignal;

export function resolveTimelineSignal(
  session: Pick<AgentSession, "session_state" | "user_state">,
  options: { connectivityHealthy?: boolean; nowMs?: number } = {},
): TimelineSignal {
  const signal = servedSignal(session.session_state, options.nowMs ?? Date.now());
  if (signal === "closed") return "closed";
  if (options.connectivityHealthy === false) return "quiet";
  // A parked or muted session does not shout; nor does it claim to be idle.
  if (signal === "attention" && !isUserActive(session)) return "unknown";
  return signal;
}

/** Spoken equivalent of the signal, so the dot's meaning reaches a11y. */
export function timelineSignalLabel(signal: TimelineSignal): string {
  switch (signal) {
    case "attention":
      return "Waiting on you";
    case "working":
      return "Working";
    case "quiet":
      return "Idle";
    case "unknown":
      return "Activity unknown";
    case "closed":
      return "Closed";
  }
}

export interface SessionRuntimeState {
  status: string | null;
  presenceState: KnownPresenceState | null;
  presenceTool: string | null;
  lastLiveAt: string | null;
  runtimeSource: string | null;
  confidence: string | null;
  displayPhase: string;
  isLive: boolean;
  isExecuting: boolean;
  needsAttention: boolean;
  isIdle: boolean;
  isStalled: boolean;
  isManagedLocalTruth: boolean;
  hasSignal: boolean;
  tone: RuntimeTone;
  stateFacts: SessionStateFacts;
}

export type SessionControlPathLabel = "Managed" | "Unmanaged";

export function resolveSessionOwnershipLabel(
  runtime: SessionRuntimeState,
): SessionControlPathLabel {
  return runtime.stateFacts.control.ownership === "owned" ? "Managed" : "Unmanaged";
}

export function normalizePresenceState(state: string | null | undefined): KnownPresenceState | null {
  return isWirePresenceState(state) ? state : null;
}

export function resolveSessionRuntimeState(
  session: TimelineRuntimeSession,
): SessionRuntimeState {
  const facts = session.session_state;
  const status = session.status ?? null;
  const presenceState = facts.activity.state === "executing"
    ? "running"
    : facts.activity.state === "quiescent"
      ? "idle"
      : normalizePresenceState(facts.activity.state);
  const presenceTool = facts.activity.tool ?? null;
  const lastLiveAt = session.last_live_at ?? session.presence_updated_at ?? null;
  const runtimeSource = session.runtime_source ?? null;
  const confidence = session.confidence ?? null;
  const tone = normalizeRuntimeTone(facts.presentation.primary?.tone) ?? "inactive";
  const displayPhase = facts.presentation.primary?.label ?? "";
  const isExecuting = facts.activity.state === "thinking" || facts.activity.state === "executing";
  const isLive = isExecuting;
  const needsAttention = needsSessionAttention(session);
  const isIdle = facts.disposition.state === "closed" || facts.activity.state === "quiescent";
  const isStalled = facts.activity.state === "stalled";

  return {
    status,
    presenceState,
    presenceTool,
    lastLiveAt,
    confidence,
    runtimeSource,
    displayPhase,
    isLive,
    isExecuting,
    needsAttention,
    isIdle,
    isStalled,
    isManagedLocalTruth: facts.mode === "helm",
    hasSignal: facts.presentation.primary != null,
    tone,
    stateFacts: facts,
  };
}

function normalizeRuntimeTone(value: string | null | undefined): RuntimeTone | null {
  if (
    value === "inactive" ||
    value === "quiet" ||
    value === "active" ||
    value === "thinking" ||
    value === "running" ||
    value === "blocked" ||
    value === "stalled" ||
    value === "idle" ||
    value === "closed"
  ) {
    return value;
  }
  return null;
}
