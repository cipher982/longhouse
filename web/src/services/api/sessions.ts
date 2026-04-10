import type { components } from "../../generated/openapi-types";
import { request } from "./base";

type SessionTurnReviewBackend = components["schemas"]["SessionTurnReviewSummary"];

// Frontend-facing camelCase interface
export interface SessionTurnReview {
  id: number;
  sessionId: string;
  assistantEventId: number;
  turnIndex: number;
  triggerType: string;
  loopMode: string;
  decision: string;
  summary: string;
  rationale: string | null;
  turnExcerpt: string | null;
  modeCapability: string | null;
  modeSummary: string | null;
  executionState: string | null;
  recommendedAction: string | null;
  followUpPrompt: string | null;
  blockedReasons: string[];
  status: string;
  reason: string | null;
  runId: number | null;
  actualOutcome: string | null;
  shadowAlignment: string | null;
  assistantTurnFinishedAt: string | null;
  turnLoopEnqueuedAt: string | null;
  turnLoopClaimedAt: string | null;
  controllerStartedAt: string | null;
  controllerCompletedAt: string | null;
  turnLoopCompletedAt: string | null;
  preEnqueueLatencyMs: number | null;
  queueLatencyMs: number | null;
  claimLatencyMs: number | null;
  controllerLatencyMs: number | null;
  reviewWriteLatencyMs: number | null;
  postReviewLatencyMs: number | null;
  workerLatencyMs: number | null;
  reviewLatencyMs: number | null;
  processingLatencyMs: number | null;
  createdAt: string;
}

// Transform snake_case backend type to camelCase frontend type
function transformSessionTurnReview(backend: SessionTurnReviewBackend): SessionTurnReview {
  return {
    id: backend.id,
    sessionId: backend.session_id,
    assistantEventId: backend.assistant_event_id,
    turnIndex: backend.turn_index,
    triggerType: backend.trigger_type,
    loopMode: backend.loop_mode,
    decision: backend.decision,
    summary: backend.summary,
    rationale: backend.rationale ?? null,
    turnExcerpt: backend.turn_excerpt ?? null,
    modeCapability: backend.mode_capability ?? null,
    modeSummary: backend.mode_summary ?? null,
    executionState: backend.execution_state ?? null,
    recommendedAction: backend.recommended_action ?? null,
    followUpPrompt: backend.follow_up_prompt ?? null,
    blockedReasons: backend.blocked_reasons,
    status: backend.status,
    reason: backend.reason ?? null,
    runId: backend.run_id ?? null,
    actualOutcome: backend.actual_outcome ?? null,
    shadowAlignment: backend.shadow_alignment ?? null,
    assistantTurnFinishedAt: backend.assistant_turn_finished_at ?? null,
    turnLoopEnqueuedAt: backend.turn_loop_enqueued_at ?? null,
    turnLoopClaimedAt: backend.turn_loop_claimed_at ?? null,
    controllerStartedAt: backend.controller_started_at ?? null,
    controllerCompletedAt: backend.controller_completed_at ?? null,
    turnLoopCompletedAt: backend.turn_loop_completed_at ?? null,
    preEnqueueLatencyMs: backend.pre_enqueue_latency_ms ?? null,
    queueLatencyMs: backend.queue_latency_ms ?? null,
    claimLatencyMs: backend.claim_latency_ms ?? null,
    controllerLatencyMs: backend.controller_latency_ms ?? null,
    reviewWriteLatencyMs: backend.review_write_latency_ms ?? null,
    postReviewLatencyMs: backend.post_review_latency_ms ?? null,
    workerLatencyMs: backend.worker_latency_ms ?? null,
    reviewLatencyMs: backend.review_latency_ms ?? null,
    processingLatencyMs: backend.processing_latency_ms ?? null,
    createdAt: backend.created_at,
  };
}

// API functions for session turn reviews
export async function fetchSessionTurnTelemetry(sessionId: string): Promise<SessionTurnReview[]> {
  const backendData = await request<SessionTurnReviewBackend[]>(`/agents/sessions/${sessionId}/turn-telemetry`);
  return backendData.map(transformSessionTurnReview);
}
