import type {
  AgentSession,
  AgentSessionStatus,
  SessionRuntimeDisplay,
  SessionStateFacts,
} from "../services/api/agents";

export type KnownPresenceState =
  | "thinking"
  | "running"
  | "idle"
  | "needs_user"
  | "blocked"
  | "stalled";
export type RuntimeTruthTier = "none" | "stale" | "fresh" | "managed-local";
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
  "ended_at" | "last_activity_at" | "timeline_anchor_at" | "capabilities" | "runtime_display" | "session_state"
> &
  Partial<Omit<TimelineRuntimeOverlay, "runtime_display">>;

export function isSessionClosed(
  session: Pick<AgentSession, "session_state"> | null | undefined,
): boolean {
  return session?.session_state.disposition.state === "closed";
}

/**
 * The single attention axis for a timeline row, mirroring iOS TimelineSignal.
 * Three semantic stops the user reads pre-attentively, plus closed:
 *   - attention: WAITING ON YOU — steady amber, never pulses.
 *   - working:   actively running — teal, pulses (live only).
 *   - quiet:     idle/stale — grey, static.
 *   - closed:    ended — dimmed, static.
 * Drives `data-signal` on the row; CSS owns the colors. Keep in lockstep with
 * `timelineSignal` in ios/.../InboxView.swift.
 */
export type TimelineSignal = "attention" | "working" | "quiet" | "unknown" | "closed";

export function resolveTimelineSignal(
  session: Pick<AgentSession, "session_state" | "user_state">,
  options: { connectivityHealthy?: boolean } = {},
): TimelineSignal {
  if (isSessionClosed(session)) return "closed";
  // A global connectivity banner owns severity; suppress per-row attention.
  if (options.connectivityHealthy === false) return "quiet";

  const facts = session.session_state;
  const userActive = session.user_state == null || session.user_state === "active";
  if (userActive && facts.pending_interaction != null) return "attention";
  if (facts.activity.state === "thinking" || facts.activity.state === "executing") return "working";
  if (facts.activity.state === "blocked" || facts.activity.state === "stalled") return "attention";
  if (facts.activity.state === "unknown") return "unknown";
  return "quiet";
}

export function getPrimaryPresentation(facts: SessionStateFacts) {
  return facts.presentation.primary;
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
  truthTier: RuntimeTruthTier;
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
  if (
    state === "thinking" ||
    state === "running" ||
    state === "idle" ||
    state === "needs_user" ||
    state === "blocked" ||
    state === "stalled"
  ) {
    return state;
  }
  return null;
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
  const truthTier = facts.activity.state === "unknown" ? "none" : "fresh";
  const tone = normalizeRuntimeTone(facts.presentation.primary?.tone) ?? "inactive";
  const displayPhase = facts.presentation.primary?.label ?? "";
  const isExecuting = facts.activity.state === "thinking" || facts.activity.state === "executing";
  const isLive = isExecuting;
  const needsAttention = facts.pending_interaction != null;
  const isIdle = facts.disposition.state === "closed" || facts.activity.state === "quiescent";
  const isStalled = facts.activity.state === "stalled";

  return {
    status,
    presenceState,
    presenceTool,
    lastLiveAt,
    confidence,
    runtimeSource,
    truthTier,
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

function normalizeRuntimeTruthTier(value: string | null | undefined): RuntimeTruthTier | null {
  if (
    value === "none" ||
    value === "stale" ||
    value === "fresh" ||
    value === "managed-local"
  ) {
    return value;
  }
  return null;
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
