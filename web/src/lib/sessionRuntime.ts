import type {
  AgentSession,
  AgentSessionStatus,
  SessionRuntimeDisplay,
} from "../services/api/agents";

export type KnownPresenceState = "thinking" | "running" | "idle" | "needs_user" | "blocked";
export type RuntimeTruthTier = "none" | "stale" | "inferred" | "fresh" | "managed-local";
export type RuntimeTone = "inactive" | "thinking" | "running" | "needs-user" | "blocked" | "idle" | "inferred";

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
  capabilities?: AgentSession["capabilities"] | null;
};

export type TimelineRuntimeSession = Pick<
  AgentSession,
  "ended_at" | "last_activity_at" | "timeline_anchor_at" | "capabilities"
> &
  Partial<TimelineRuntimeOverlay>;

/**
 * Phase 3 of session-liveness-honesty: a single place for deciding whether
 * a session is closed. Callers that previously gated on `session.ended_at`
 * or `session.terminal_state` should use this instead.
 *
 * Contract:
 * - When `runtime_display.lifecycle` is present, it is the ground truth.
 *   Backend only emits `closed` on explicit terminal signals (Phase 6 adds
 *   process-gone).
 * - Older payloads without the axis fall back to `terminal_state`.
 */
export function isSessionClosed(
  session: Pick<AgentSession, "terminal_state"> & {
    runtime_display?: SessionRuntimeDisplay | null;
  },
): boolean {
  const lifecycle = session.runtime_display?.lifecycle;
  if (lifecycle != null) {
    return lifecycle === "closed";
  }
  return !!session.terminal_state;
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
  heuristicActive: boolean;
  isManagedLocalTruth: boolean;
  hasSignal: boolean;
  tone: RuntimeTone;
  runtimeDisplay: SessionRuntimeDisplay | null;
}

export type SessionControlPathLabel = "Managed" | "Unmanaged";

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

export function resolveSessionStatusLabel(
  runtime: SessionRuntimeState,
  fallbackControlPath: "managed" | "unmanaged" = "unmanaged",
): string {
  const display = runtime.runtimeDisplay;
  if (display?.lifecycle === "closed") {
    return "Closed";
  }

  const controlPath = display?.control_path ?? fallbackControlPath;
  if (controlPath === "managed") {
    if (runtime.presenceState === "running" || runtime.presenceState === "thinking") {
      return "Working";
    }
    if (runtime.presenceState === "needs_user" || runtime.presenceState === "blocked") {
      return "Needs you";
    }
    if (runtime.presenceState === "idle" || runtime.isIdle) {
      return "Ready";
    }
    if (display?.activity_recency === "live" || display?.activity_recency === "recent" || runtime.heuristicActive) {
      return "Recent activity";
    }
    if (display?.activity_recency === "none") {
      return "Unknown";
    }
    return "Disconnected";
  }

  if (controlPath === "unmanaged") {
    if (display?.activity_recency === "live" || runtime.isExecuting || runtime.needsAttention || runtime.heuristicActive) {
      return "Active";
    }
    if (display?.activity_recency === "recent") {
      return "Recent activity";
    }
    if (display?.activity_recency === "stale") {
      return "Stale";
    }
    if (display?.host_state === "online") {
      return "Host online";
    }
    return "Unknown";
  }

  if (runtime.isExecuting || runtime.needsAttention || runtime.heuristicActive) {
    return "Active";
  }
  if (runtime.isIdle) {
    return "Ready";
  }
  return "Unknown";
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

function isLegacyProgressStatus(status: string | null): boolean {
  return status === "working" || status === "active";
}

function isProgressFallback({
  status,
  confidence,
  runtimeSource,
  presenceState,
}: {
  status: string | null;
  confidence: string | null;
  runtimeSource: string | null;
  presenceState: KnownPresenceState | null;
}): boolean {
  if (presenceState != null) {
    return false;
  }
  return confidence === "inferred" || runtimeSource === "progress" || isLegacyProgressStatus(status);
}

function hasFreshRuntimeSignal({
  confidence,
  runtimeSource,
  presenceState,
}: {
  confidence: string | null;
  runtimeSource: string | null;
  presenceState: KnownPresenceState | null;
}): boolean {
  return (
    presenceState != null ||
    (confidence === "live" && runtimeSource !== "progress" && runtimeSource !== "fallback") ||
    runtimeSource === "semantic" ||
    runtimeSource === "managed_local_transport"
  );
}

function getRuntimeTruthTier(
  overlay: Partial<TimelineRuntimeOverlay> | null | undefined,
): RuntimeTruthTier {
  const status = overlay?.status ?? null;
  const confidence = overlay?.confidence ?? null;
  const presenceState = normalizePresenceState(overlay?.presence_state ?? null);
  const runtimeSource = normalizeRuntimeSource(overlay?.runtime_source ?? null);
  const hostReattachAvailable = overlay?.capabilities?.host_reattach_available === true;
  const hasFreshSignal = hasFreshRuntimeSignal({ confidence, runtimeSource, presenceState });

  if (hostReattachAvailable && hasFreshSignal && confidence !== "stale") {
    return "managed-local";
  }
  if (hasFreshSignal && confidence !== "stale") {
    return "fresh";
  }
  if (isProgressFallback({ status, confidence, runtimeSource, presenceState })) {
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

  if (isLegacyProgressStatus(status)) return "Recent progress";
  if (status === "idle") return "Idle";
  // Phase 1 of session-liveness-honesty: do not treat fallbackEndedAt as
  // Completed — that field is just the last-activity timestamp for unmanaged
  // sessions. Only an explicit "completed" status (which the backend now
  // gates on terminal_state) means the process is actually closed.
  if (status === "completed") return "Completed";
  return "Recent";
}

function getTone(
  presenceState: KnownPresenceState | null,
  {
    heuristicActive,
    isIdle,
  }: {
    heuristicActive: boolean;
    isIdle: boolean;
  },
): SessionRuntimeState["tone"] {
  if (presenceState === "blocked") {
    return "blocked";
  }
  if (presenceState === "needs_user") {
    return "needs-user";
  }
  if (presenceState === "running") {
    return "running";
  }
  if (presenceState === "thinking") {
    return "thinking";
  }
  if (heuristicActive) {
    return "inferred";
  }
  if (isIdle) {
    return "idle";
  }
  return "inactive";
}

export function resolveSessionRuntimeState(
  session: TimelineRuntimeSession,
): SessionRuntimeState {
  const serverDisplay = session.runtime_display ?? null;
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
  const truthTier = normalizeRuntimeTruthTier(serverDisplay?.truth_tier) ?? sessionTruthTier;

  const heuristicActive = serverDisplay?.heuristic_active ?? isProgressFallback({ status, confidence, runtimeSource, presenceState });
  const isExecuting = serverDisplay?.is_executing ?? (presenceState === "thinking" || presenceState === "running");
  const needsAttention = serverDisplay?.needs_attention ?? (presenceState === "needs_user" || presenceState === "blocked");

  const isLive = serverDisplay?.is_live ?? isExecuting;
  const isIdle = serverDisplay?.is_idle ?? (presenceState === "idle" || (!isExecuting && !needsAttention && !heuristicActive && status === "idle"));
  const hasSignal = serverDisplay?.has_signal ?? (truthTier !== "none" || presenceState != null || status != null || lastLiveAt != null);

  const displayPhase =
    serverDisplay?.phase_label ??
    getDisplayPhase(
      presenceState,
      presenceTool,
      status,
      // Phase 1: do not feed ended_at as a terminal hint. See getDisplayPhase.
      null,
      session.display_phase ?? null,
    );
  const tone = normalizeRuntimeTone(serverDisplay?.tone) ?? getTone(presenceState, { heuristicActive, isIdle });

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
    isManagedLocalTruth: serverDisplay?.is_managed_local_truth ?? truthTier === "managed-local",
    hasSignal,
    tone,
    runtimeDisplay: serverDisplay,
  };
}

function normalizeRuntimeTruthTier(value: string | null | undefined): RuntimeTruthTier | null {
  if (
    value === "none" ||
    value === "stale" ||
    value === "inferred" ||
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
    value === "thinking" ||
    value === "running" ||
    value === "needs-user" ||
    value === "blocked" ||
    value === "idle" ||
    value === "inferred"
  ) {
    return value;
  }
  return null;
}
