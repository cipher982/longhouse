import type {
  AgentSession,
  AgentSessionStatus,
  ManagedSessionTransport,
  SessionExecutionHome,
} from "../services/api/agents";

export type KnownPresenceState = "thinking" | "running" | "idle" | "needs_user" | "blocked";
export type RuntimeTruthTier = "none" | "stale" | "inferred" | "fresh" | "managed-local";

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
  execution_home?: SessionExecutionHome | null;
  managed_transport?: ManagedSessionTransport | null;
};

export type TimelineRuntimeSession = Pick<
  AgentSession,
  "ended_at" | "last_activity_at" | "timeline_anchor_at" | "execution_home" | "managed_transport"
> &
  Partial<TimelineRuntimeOverlay>;

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
  heuristicActive: boolean;
  isManagedLocalTruth: boolean;
  hasSignal: boolean;
  tone: "inactive" | "thinking" | "running" | "needs-user" | "blocked" | "idle" | "inferred";
}

export function normalizePresenceState(state: string | null | undefined): KnownPresenceState | null {
  if (
    state === "thinking" ||
    state === "running" ||
    state === "idle" ||
    state === "needs_user" ||
    state === "blocked"
  ) {
    return state;
  }
  return null;
}

function normalizeRuntimeSource(source: string | null | undefined): string | null {
  const trimmed = source?.trim();
  return trimmed ? trimmed : null;
}

function getRuntimeTruthTier(
  overlay: Partial<TimelineRuntimeOverlay> | null | undefined,
): RuntimeTruthTier {
  const status = overlay?.status ?? null;
  const confidence = overlay?.confidence ?? null;
  const presenceState = normalizePresenceState(overlay?.presence_state ?? null);
  const runtimeSource = normalizeRuntimeSource(overlay?.runtime_source ?? null);
  const executionHome = overlay?.execution_home ?? null;
  const statusSuggestsRecentProgress =
    status === "working" || status === "thinking" || status === "active";
  const hasFreshSignal =
    presenceState != null ||
    (confidence === "live" && runtimeSource !== "progress" && runtimeSource !== "fallback") ||
    runtimeSource === "semantic" ||
    runtimeSource === "managed_local_transport";

  if (executionHome === "managed_local" && hasFreshSignal && confidence !== "stale") {
    return "managed-local";
  }
  if (hasFreshSignal && confidence !== "stale") {
    return "fresh";
  }
  if (confidence === "inferred" || runtimeSource === "progress" || statusSuggestsRecentProgress) {
    return "inferred";
  }
  if (confidence === "stale" || runtimeSource === "fallback") {
    return "stale";
  }
  return "none";
}

function getDisplayPhase(
  presenceState: KnownPresenceState | null,
  presenceTool: string | null,
  status: string | null,
  fallbackEndedAt: string | null,
  explicitDisplayPhase?: string | null,
): string {
  if (explicitDisplayPhase?.trim()) {
    return explicitDisplayPhase.trim();
  }

  if (presenceState === "running") {
    return presenceTool ? `Running ${presenceTool}` : "Running";
  }
  if (presenceState === "thinking") {
    return "Thinking";
  }
  if (presenceState === "needs_user") {
    return "Needs you";
  }
  if (presenceState === "blocked") {
    return presenceTool ? `Blocked on ${presenceTool}` : "Needs permission";
  }
  if (presenceState === "idle") {
    return "Idle";
  }

  // Compatibility shim: older callers may still emit `working` without a
  // semantic presence state. Treat that as fallback progress until all
  // producers are tightened to the newer execution-only contract.
  if (status === "working") return "Recent progress";
  if (status === "thinking") return "Thinking";
  if (status === "active") return "Recent progress";
  if (status === "idle") return "Idle";
  if (status === "completed" || fallbackEndedAt != null) return "Completed";
  return "Recent";
}

export function resolveSessionRuntimeState(
  session: TimelineRuntimeSession,
): SessionRuntimeState {
  const sessionTruthTier = getRuntimeTruthTier(session);
  const status = session.status ?? null;
  const presenceState = normalizePresenceState(session.presence_state ?? null);
  const presenceTool =
    session.active_tool ??
    session.presence_tool ??
    null;
  const lastLiveAt =
    session.last_live_at ??
    session.presence_updated_at ??
    (presenceState ? session.last_activity_at ?? null : null);
  const runtimeSource = normalizeRuntimeSource(session.runtime_source ?? null);
  const confidence = session.confidence ?? null;
  const truthTier = sessionTruthTier;

  const heuristicActive = presenceState == null && truthTier === "inferred";
  const isExecuting = presenceState === "thinking" || presenceState === "running";
  const needsAttention = presenceState === "needs_user" || presenceState === "blocked";

  const isLive = isExecuting;
  const isIdle = presenceState === "idle" || (!isExecuting && !needsAttention && !heuristicActive && status === "idle");
  const hasSignal =
    truthTier !== "none" ||
    presenceState != null ||
    status != null ||
    lastLiveAt != null ||
    heuristicActive;

  let tone: SessionRuntimeState["tone"] = "inactive";
  if (presenceState === "blocked") {
    tone = "blocked";
  } else if (presenceState === "needs_user") {
    tone = "needs-user";
  } else if (presenceState === "running") {
    tone = "running";
  } else if (presenceState === "thinking") {
    tone = "thinking";
  } else if (heuristicActive) {
    tone = "inferred";
  } else if (isIdle) {
    tone = "idle";
  }

  const displayPhase = getDisplayPhase(
    presenceState,
    presenceTool,
    status,
    session.ended_at ?? null,
    session.display_phase ?? null,
  );

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
    heuristicActive,
    isManagedLocalTruth: truthTier === "managed-local",
    hasSignal,
    tone,
  };
}
