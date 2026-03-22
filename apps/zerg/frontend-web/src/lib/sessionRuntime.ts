import type {
  AgentActiveSession,
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

export type TimelineRuntimeActiveSession = Pick<
  AgentActiveSession,
  | "status"
  | "ended_at"
  | "presence_state"
  | "presence_tool"
  | "presence_updated_at"
  | "last_activity_at"
  | "timeline_anchor_at"
  | "runtime_source"
  | "last_live_at"
  | "display_phase"
  | "active_tool"
  | "confidence"
  | "execution_home"
  | "managed_transport"
>;

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

function overlayFreshnessMillis(overlay: Partial<TimelineRuntimeOverlay> & { last_activity_at?: string | null } | null | undefined): number {
  const timestamp =
    overlay?.last_live_at ??
    overlay?.presence_updated_at ??
    overlay?.timeline_anchor_at ??
    overlay?.last_activity_at ??
    null;
  if (!timestamp) {
    return Number.NEGATIVE_INFINITY;
  }
  const millis = new Date(timestamp).getTime();
  return Number.isFinite(millis) ? millis : Number.NEGATIVE_INFINITY;
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

function truthTierScore(tier: RuntimeTruthTier): number {
  switch (tier) {
    case "managed-local":
      return 4;
    case "fresh":
      return 3;
    case "inferred":
      return 2;
    case "stale":
      return 1;
    default:
      return 0;
  }
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

  if (status === "working") return "Recent progress";
  if (status === "thinking") return "Thinking";
  if (status === "active") return "Recent progress";
  if (status === "idle") return "Idle";
  if (status === "completed" || fallbackEndedAt != null) return "Completed";
  return "Recent";
}

export function resolveSessionRuntimeState(
  session: TimelineRuntimeSession,
  activeSession?: TimelineRuntimeActiveSession | null,
): SessionRuntimeState {
  const sessionTruthTier = getRuntimeTruthTier(session);
  const activeTruthTier = getRuntimeTruthTier(activeSession);
  const sessionScore = truthTierScore(sessionTruthTier);
  const activeScore = truthTierScore(activeTruthTier);
  const sessionWins =
    sessionScore > activeScore ||
    (sessionScore === activeScore && overlayFreshnessMillis(session) >= overlayFreshnessMillis(activeSession));
  const primaryOverlay = sessionWins ? session : activeSession;
  const secondaryOverlay = sessionWins ? activeSession : session;

  const status = primaryOverlay?.status ?? secondaryOverlay?.status ?? null;
  const presenceState = normalizePresenceState(
    primaryOverlay?.presence_state ?? secondaryOverlay?.presence_state ?? null,
  );
  const presenceTool =
    primaryOverlay?.active_tool ??
    primaryOverlay?.presence_tool ??
    secondaryOverlay?.active_tool ??
    secondaryOverlay?.presence_tool ??
    null;
  const lastLiveAt =
    primaryOverlay?.last_live_at ??
    primaryOverlay?.presence_updated_at ??
    secondaryOverlay?.last_live_at ??
    secondaryOverlay?.presence_updated_at ??
    (presenceState ? primaryOverlay?.last_activity_at ?? secondaryOverlay?.last_activity_at ?? null : null);
  const runtimeSource = normalizeRuntimeSource(
    primaryOverlay?.runtime_source ?? secondaryOverlay?.runtime_source ?? null,
  );
  const confidence =
    primaryOverlay?.confidence ??
    secondaryOverlay?.confidence ??
    null;
  const truthTier = sessionWins ? sessionTruthTier : activeTruthTier;

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
    activeSession?.ended_at ?? session.ended_at ?? null,
    primaryOverlay?.display_phase ?? secondaryOverlay?.display_phase ?? null,
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
