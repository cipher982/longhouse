import type {
  AgentSession,
  AgentSessionStatus,
  SessionRuntimeDisplay,
} from "../services/api/agents";

export type KnownPresenceState =
  | "thinking"
  | "running"
  | "idle"
  | "needs_user"
  | "blocked"
  | "stalled"
  | "syncing_transcript";
export type RuntimeTruthTier = "none" | "stale" | "fresh" | "managed-local";
export type RuntimeTone = "inactive" | "active" | "thinking" | "running" | "blocked" | "stalled" | "idle" | "closed";

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
  "ended_at" | "last_activity_at" | "timeline_anchor_at" | "capabilities" | "runtime_display"
> &
  Partial<Omit<TimelineRuntimeOverlay, "runtime_display">>;

export function isSessionClosed(
  session: Pick<AgentSession, "runtime_display"> | null | undefined,
): boolean {
  return session?.runtime_display?.lifecycle === "closed";
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
  runtimeDisplay: SessionRuntimeDisplay;
}

export type SessionControlPathLabel = "Managed" | "Unmanaged";

export function resolveSessionOwnershipLabel(
  runtime: SessionRuntimeState,
): SessionControlPathLabel {
  return runtime.runtimeDisplay.control_path === "managed" ? "Managed" : "Unmanaged";
}

export function normalizePresenceState(state: string | null | undefined): KnownPresenceState | null {
  if (
    state === "thinking" ||
    state === "running" ||
    state === "idle" ||
    state === "needs_user" ||
    state === "blocked" ||
    state === "stalled" ||
    state === "syncing_transcript"
  ) {
    return state;
  }
  return null;
}

export function resolveSessionRuntimeState(
  session: TimelineRuntimeSession,
): SessionRuntimeState {
  const serverDisplay = session.runtime_display;
  const status = session.status ?? null;
  const presenceState = normalizePresenceState(serverDisplay.state);
  const presenceTool = serverDisplay.compact_tool_label ?? null;
  const lastLiveAt = session.last_live_at ?? session.presence_updated_at ?? null;
  const runtimeSource = session.runtime_source ?? null;
  const confidence = session.confidence ?? null;
  const truthTier = normalizeRuntimeTruthTier(serverDisplay.truth_tier) ?? "none";
  const tone = normalizeRuntimeTone(serverDisplay.tone) ?? "inactive";
  const displayPhase = serverDisplay.phase_label;
  const isLive = serverDisplay.is_live;
  const isExecuting = serverDisplay.is_executing;
  const needsAttention = serverDisplay.needs_attention;
  const isIdle = serverDisplay.is_idle;
  const isStalled = serverDisplay.is_stalled ?? false;

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
    isManagedLocalTruth: serverDisplay.is_managed_local_truth,
    hasSignal: serverDisplay.has_signal,
    tone,
    runtimeDisplay: serverDisplay,
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
