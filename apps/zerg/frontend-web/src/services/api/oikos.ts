import { request } from "./base";
import type { SessionLoopMode } from "./agents";

interface OikosWakeupSummary {
  id: number;
  source: string;
  trigger_type: string;
  status: string;
  reason: string | null;
  session_id: string | null;
  conversation_id: string | null;
  wakeup_key: string | null;
  run_id: number | null;
  payload: Record<string, unknown> | null;
  created_at: string;
}

export type SessionLoopModeCapability =
  | "observe_only"
  | "notify_only"
  | "bounded_autonomy"
  | (string & {});

export type SessionLoopExecutionState =
  | "observe_only"
  | "awaiting_user_approval"
  | "would_auto_continue"
  | "needs_human"
  | "no_action"
  | (string & {});

export type SessionShadowOutcome =
  | "ignore"
  | "notify_user"
  | "continue_session"
  | "delegated_follow_up"
  | "failed"
  | (string & {});

export type SessionShadowAlignment =
  | "matched"
  | "more_conservative"
  | "more_aggressive"
  | "different"
  | "failed"
  | (string & {});

export type SessionShadowReadiness =
  | "no_signal"
  | "early"
  | "promising"
  | "caution"
  | (string & {});

export interface SessionShadowReview {
  generatedAt: string;
  triggerType: string;
  decision: string;
  summary: string;
  rationale: string;
  needsHuman: boolean;
  loopMode: SessionLoopMode;
  modeCapability: SessionLoopModeCapability;
  modeSummary: string;
  executionState: SessionLoopExecutionState;
  wouldNotifyUser: boolean;
  wouldContinueSession: boolean;
  blockedReasons: string[];
  recommendedAction: string | null;
  wakeupStatus: string;
  wakeupReason: string | null;
  actualOutcome: SessionShadowOutcome | null;
  expectedOutcome: SessionShadowOutcome | null;
  alignment: SessionShadowAlignment | null;
}

export interface SessionShadowRollup {
  totalReviews: number;
  pendingReviews: number;
  matched: number;
  moreConservative: number;
  moreAggressive: number;
  different: number;
  failed: number;
  readiness: SessionShadowReadiness;
}

export interface SessionShadowTelemetry {
  latestReview: SessionShadowReview | null;
  rollup: SessionShadowRollup | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (typeof item === "string" ? item.trim() : ""))
    .filter((item) => item.length > 0);
}

function parseShadowReview(wakeup: OikosWakeupSummary): SessionShadowReview | null {
  if (!isRecord(wakeup.payload)) return null;
  const shadowReview = wakeup.payload.shadow_review;
  if (!isRecord(shadowReview)) return null;

  const decision = isRecord(shadowReview.decision) ? shadowReview.decision : null;
  const loopReview = isRecord(shadowReview.loop_review) ? shadowReview.loop_review : null;
  const context = isRecord(shadowReview.context) ? shadowReview.context : null;
  const trigger = context && isRecord(context.trigger) ? context.trigger : null;

  if (!decision || !loopReview) return null;

  const generatedAt = asString(shadowReview.generated_at) ?? wakeup.created_at;
  const loopMode = (asString(loopReview.loop_mode) ?? "manual") as SessionLoopMode;

  return {
    generatedAt,
    triggerType: asString(trigger?.type) ?? wakeup.trigger_type,
    decision: asString(decision.decision) ?? "ignore",
    summary: asString(decision.summary) ?? "No follow-up action needed.",
    rationale: asString(decision.rationale) ?? "",
    needsHuman: asBoolean(decision.needs_human),
    loopMode,
    modeCapability: (asString(loopReview.mode_capability) ?? "observe_only") as SessionLoopModeCapability,
    modeSummary: asString(loopReview.mode_summary) ?? "",
    executionState: (asString(loopReview.execution_state) ?? "no_action") as SessionLoopExecutionState,
    wouldNotifyUser: asBoolean(loopReview.would_notify_user),
    wouldContinueSession: asBoolean(loopReview.would_continue_session),
    blockedReasons: asStringArray(loopReview.blocked_reasons),
    recommendedAction: asString(loopReview.recommended_action),
    wakeupStatus: wakeup.status,
    wakeupReason: wakeup.reason,
    actualOutcome: (asString(wakeup.payload.outcome) ?? null) as SessionShadowOutcome | null,
    expectedOutcome: (asString(wakeup.payload.shadow_expected_outcome) ?? null) as SessionShadowOutcome | null,
    alignment: (asString(wakeup.payload.shadow_alignment) ?? null) as SessionShadowAlignment | null,
  };
}

function buildSessionShadowRollup(reviews: SessionShadowReview[]): SessionShadowRollup | null {
  if (reviews.length === 0) return null;

  const rollup: SessionShadowRollup = {
    totalReviews: 0,
    pendingReviews: 0,
    matched: 0,
    moreConservative: 0,
    moreAggressive: 0,
    different: 0,
    failed: 0,
    readiness: "no_signal",
  };

  for (const review of reviews) {
    if (!review.alignment) {
      rollup.pendingReviews += 1;
      continue;
    }

    rollup.totalReviews += 1;
    if (review.alignment === "matched") rollup.matched += 1;
    if (review.alignment === "more_conservative") rollup.moreConservative += 1;
    if (review.alignment === "more_aggressive") rollup.moreAggressive += 1;
    if (review.alignment === "different") rollup.different += 1;
    if (review.alignment === "failed") rollup.failed += 1;
  }

  const cautionCount = rollup.moreAggressive + rollup.different + rollup.failed;
  if (rollup.totalReviews === 0) {
    rollup.readiness = "no_signal";
  } else if (cautionCount > 0) {
    rollup.readiness = "caution";
  } else if (rollup.matched >= 3) {
    rollup.readiness = "promising";
  } else {
    rollup.readiness = "early";
  }

  return rollup;
}

export async function fetchSessionShadowTelemetry(
  sessionId: string,
): Promise<SessionShadowTelemetry> {
  const params = new URLSearchParams({
    session_id: sessionId,
    limit: "25",
  });
  const wakeups = await request<OikosWakeupSummary[]>(`/oikos/wakeups?${params.toString()}`);
  const reviews = wakeups
    .map(parseShadowReview)
    .filter((review): review is SessionShadowReview => review !== null);

  return {
    latestReview: reviews[0] ?? null,
    rollup: buildSessionShadowRollup(reviews),
  };
}

export async function fetchLatestSessionShadowReview(
  sessionId: string,
): Promise<SessionShadowReview | null> {
  const telemetry = await fetchSessionShadowTelemetry(sessionId);
  return telemetry.latestReview;
}
