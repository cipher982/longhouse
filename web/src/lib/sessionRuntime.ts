import type {
  AgentSession,
  AgentSessionStatus,
  SessionLivenessFacts,
  SessionRuntimeDisplay,
} from "../services/api/agents";

export type KnownPresenceState = "thinking" | "running" | "idle" | "needs_user" | "blocked" | "stalled";
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
  runtime_display?: SessionRuntimeDisplay | null;
  runtime_facts?: SessionLivenessFacts | null;
  capabilities?: AgentSession["capabilities"] | null;
};

export type TimelineRuntimeSession = Pick<
  AgentSession,
  "ended_at" | "last_activity_at" | "timeline_anchor_at" | "capabilities"
> &
  Partial<TimelineRuntimeOverlay>;

/** Decide closed/open only from explicit lifecycle or terminal facts. */
export function isSessionClosed(
  session: Pick<AgentSession, "terminal_state"> & {
    runtime_display?: SessionRuntimeDisplay | null;
    runtime_facts?: SessionLivenessFacts | null;
  },
): boolean {
  const lifecycle = session.runtime_display?.lifecycle;
  if (lifecycle != null) {
    return lifecycle === "closed";
  }
  return (
    session.terminal_state === "session_ended" ||
    session.terminal_state === "process_gone" ||
    session.terminal_state === "user_closed"
  );
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
  runtimeDisplay: SessionRuntimeDisplay | null;
  runtimeFacts: SessionLivenessFacts | null;
  factStatus: SessionFactStatus | null;
}

export type SessionControlPathLabel = "Managed" | "Unmanaged";

export interface SessionFactStatus {
  label: string;
  tone: RuntimeTone;
  seenAt: string | null;
  seenAtPrefix: "Closed" | "Checked" | "Last signal" | "Updated" | "Verified";
}

export function resolveSessionOwnershipLabel(
  runtime: SessionRuntimeState,
  fallback: SessionControlPathLabel = "Unmanaged",
): SessionControlPathLabel {
  const controlPath = runtime.runtimeDisplay?.control_path;
  if (controlPath === "managed") {
    return "Managed";
  }
  if (controlPath === "unmanaged") {
    return "Unmanaged";
  }
  return fallback;
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
  const serverDisplay = session.runtime_display ?? null;
  const runtimeFacts = session.runtime_facts ?? null;
  const status = session.status ?? null;
  if (!serverDisplay) {
    return {
      status,
      presenceState: null,
      presenceTool: null,
      lastLiveAt: null,
      confidence: null,
      runtimeSource: null,
      truthTier: "none",
      displayPhase: "Inactive",
      isLive: false,
      isExecuting: false,
      needsAttention: false,
      isIdle: false,
      isStalled: false,
      isManagedLocalTruth: false,
      hasSignal: false,
      tone: "inactive",
      runtimeDisplay: null,
      runtimeFacts,
      factStatus: null,
    };
  }

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
    runtimeFacts,
    factStatus: null,
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
