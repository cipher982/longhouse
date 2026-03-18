import { request } from "./base";
import type { SessionLoopMode } from "./agents";

export type SessionTurnDecision =
  | "continue"
  | "ask_user"
  | "wait"
  | "done"
  | "escalate"
  | (string & {});

export type SessionLoopModeCapability =
  | "observe_only"
  | "notify_only"
  | "bounded_autonomy"
  | (string & {});

export type SessionTurnExecutionState =
  | "observe_only"
  | "awaiting_user_approval"
  | "would_auto_continue"
  | "needs_human"
  | "no_action"
  | (string & {});

export type SessionTurnOutcome =
  | "ignore"
  | "notify_user"
  | "continue_session"
  | "delegated_follow_up"
  | "failed"
  | (string & {});

export type SessionTurnAlignment =
  | "matched"
  | "more_conservative"
  | "more_aggressive"
  | "different"
  | "failed"
  | (string & {});

export type SessionTurnStability =
  | "no_signal"
  | "developing"
  | "steady"
  | "caution"
  | (string & {});

interface SessionTurnReviewSummary {
  id: number;
  session_id: string;
  assistant_event_id: number;
  turn_index: number;
  trigger_type: string;
  loop_mode: string;
  decision: string;
  summary: string;
  rationale: string | null;
  turn_excerpt: string | null;
  mode_capability: string | null;
  mode_summary: string | null;
  execution_state: string | null;
  recommended_action: string | null;
  blocked_reasons: string[] | null;
  status: string;
  reason: string | null;
  run_id: number | null;
  actual_outcome: string | null;
  shadow_alignment: string | null;
  created_at: string;
}

export interface SessionTurnReview {
  id: number;
  sessionId: string;
  assistantEventId: number;
  turnIndex: number;
  triggerType: string;
  loopMode: SessionLoopMode;
  decision: SessionTurnDecision;
  summary: string;
  rationale: string;
  turnExcerpt: string | null;
  modeCapability: SessionLoopModeCapability;
  modeSummary: string;
  executionState: SessionTurnExecutionState;
  recommendedAction: string | null;
  blockedReasons: string[];
  status: string;
  reason: string | null;
  runId: number | null;
  actualOutcome: SessionTurnOutcome | null;
  alignment: SessionTurnAlignment | null;
  createdAt: string;
}

export interface SessionTurnRollup {
  totalReviews: number;
  pendingReviews: number;
  matched: number;
  moreConservative: number;
  moreAggressive: number;
  different: number;
  failed: number;
  stability: SessionTurnStability;
}

export interface SessionTurnTelemetry {
  latestReview: SessionTurnReview | null;
  rollup: SessionTurnRollup | null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (typeof item === "string" ? item.trim() : ""))
    .filter((item) => item.length > 0);
}

function parseTurnReview(row: SessionTurnReviewSummary): SessionTurnReview {
  return {
    id: row.id,
    sessionId: row.session_id,
    assistantEventId: row.assistant_event_id,
    turnIndex: row.turn_index,
    triggerType: row.trigger_type,
    loopMode: (asString(row.loop_mode) ?? "manual") as SessionLoopMode,
    decision: (asString(row.decision) ?? "done") as SessionTurnDecision,
    summary: asString(row.summary) ?? "No follow-up action needed.",
    rationale: asString(row.rationale) ?? "",
    turnExcerpt: asString(row.turn_excerpt),
    modeCapability: (asString(row.mode_capability) ?? "observe_only") as SessionLoopModeCapability,
    modeSummary: asString(row.mode_summary) ?? "",
    executionState: (asString(row.execution_state) ?? "no_action") as SessionTurnExecutionState,
    recommendedAction: asString(row.recommended_action),
    blockedReasons: asStringArray(row.blocked_reasons),
    status: asString(row.status) ?? "recorded",
    reason: asString(row.reason),
    runId: asNumber(row.run_id),
    actualOutcome: (asString(row.actual_outcome) ?? null) as SessionTurnOutcome | null,
    alignment: (asString(row.shadow_alignment) ?? null) as SessionTurnAlignment | null,
    createdAt: row.created_at,
  };
}

function buildSessionTurnRollup(reviews: SessionTurnReview[]): SessionTurnRollup | null {
  if (reviews.length === 0) return null;

  const rollup: SessionTurnRollup = {
    totalReviews: 0,
    pendingReviews: 0,
    matched: 0,
    moreConservative: 0,
    moreAggressive: 0,
    different: 0,
    failed: 0,
    stability: "no_signal",
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
    rollup.stability = "no_signal";
  } else if (cautionCount > 0) {
    rollup.stability = "caution";
  } else if (rollup.matched >= 3) {
    rollup.stability = "steady";
  } else {
    rollup.stability = "developing";
  }

  return rollup;
}

export async function fetchSessionTurnTelemetry(sessionId: string): Promise<SessionTurnTelemetry> {
  const params = new URLSearchParams({
    session_id: sessionId,
    limit: "25",
  });
  const reviews = await request<SessionTurnReviewSummary[]>(`/oikos/turn-reviews?${params.toString()}`);
  const parsed = reviews.map(parseTurnReview);
  return {
    latestReview: parsed[0] ?? null,
    rollup: buildSessionTurnRollup(parsed),
  };
}
