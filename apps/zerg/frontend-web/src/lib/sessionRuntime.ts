import type {
  AgentActiveSession,
  AgentSession,
  AgentSessionStatus,
} from "../services/api/agents";

export type KnownPresenceState = "thinking" | "running" | "idle" | "needs_user" | "blocked";

type TimelineRuntimeOverlay = {
  status?: AgentSessionStatus | string | null;
  presence_state?: string | null;
  presence_tool?: string | null;
  presence_updated_at?: string | null;
  last_live_at?: string | null;
  display_phase?: string | null;
  active_tool?: string | null;
  confidence?: string | null;
};

export type TimelineRuntimeSession = Pick<AgentSession, "ended_at" | "last_activity_at"> &
  Partial<TimelineRuntimeOverlay>;

export type TimelineRuntimeActiveSession = Pick<
  AgentActiveSession,
  "status" | "ended_at" | "presence_state" | "presence_tool" | "presence_updated_at" | "last_activity_at"
>;

export interface SessionRuntimeState {
  status: string | null;
  presenceState: KnownPresenceState | null;
  presenceTool: string | null;
  lastLiveAt: string | null;
  confidence: string | null;
  displayPhase: string;
  isLive: boolean;
  isIdle: boolean;
  heuristicActive: boolean;
  hasSignal: boolean;
  tone: "inactive" | "live" | "running" | "needs-user" | "blocked" | "idle";
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

  if (status === "working") return "Working";
  if (status === "thinking") return "Thinking";
  if (status === "active") return "Active";
  if (status === "idle") return "Idle";
  if (status === "completed" || fallbackEndedAt != null) return "Completed";
  return "Recent";
}

export function resolveSessionRuntimeState(
  session: TimelineRuntimeSession,
  activeSession?: TimelineRuntimeActiveSession | null,
): SessionRuntimeState {
  const status = activeSession?.status ?? session.status ?? null;
  const presenceState = normalizePresenceState(
    activeSession?.presence_state ?? session.presence_state ?? null,
  );
  const presenceTool =
    activeSession?.presence_tool ??
    session.active_tool ??
    session.presence_tool ??
    null;
  const lastLiveAt =
    activeSession?.presence_updated_at ??
    session.presence_updated_at ??
    session.last_live_at ??
    (presenceState ? activeSession?.last_activity_at ?? session.last_activity_at ?? null : null);
  const confidence = session.confidence ?? (activeSession ? "live" : null);

  const statusSuggestsLive =
    status === "working" || status === "thinking" || status === "active";
  const openSessionFallback =
    activeSession == null &&
    status == null &&
    presenceState == null &&
    session.ended_at == null;
  const heuristicActive = presenceState == null && (statusSuggestsLive || openSessionFallback);

  const isLive =
    presenceState === "thinking" ||
    presenceState === "running" ||
    presenceState === "needs_user" ||
    presenceState === "blocked" ||
    heuristicActive;
  const isIdle = presenceState === "idle" || (!isLive && status === "idle");
  const hasSignal =
    activeSession != null ||
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
  } else if (isLive) {
    tone = "live";
  } else if (isIdle) {
    tone = "idle";
  }

  const displayPhase = getDisplayPhase(
    presenceState,
    presenceTool,
    status,
    activeSession?.ended_at ?? session.ended_at ?? null,
    session.display_phase,
  );

  return {
    status,
    presenceState,
    presenceTool,
    lastLiveAt,
    confidence,
    displayPhase: heuristicActive && displayPhase === "Recent" ? "Active" : displayPhase,
    isLive,
    isIdle,
    heuristicActive,
    hasSignal,
    tone,
  };
}
