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

export type LoopCardState =
  | "active"
  | "acted"
  | "dismissed"
  | "superseded"
  | "expired"
  | "failed"
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
  follow_up_prompt: string | null;
  blocked_reasons: string[] | null;
  status: string;
  reason: string | null;
  run_id: number | null;
  actual_outcome: string | null;
  shadow_alignment: string | null;
  assistant_turn_finished_at: string | null;
  turn_loop_enqueued_at: string | null;
  turn_loop_completed_at: string | null;
  queue_latency_ms: number | null;
  review_latency_ms: number | null;
  processing_latency_ms: number | null;
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
  followUpPrompt: string | null;
  blockedReasons: string[];
  status: string;
  reason: string | null;
  runId: number | null;
  actualOutcome: SessionTurnOutcome | null;
  alignment: SessionTurnAlignment | null;
  assistantTurnFinishedAt: string | null;
  turnLoopEnqueuedAt: string | null;
  turnLoopCompletedAt: string | null;
  queueLatencyMs: number | null;
  reviewLatencyMs: number | null;
  processingLatencyMs: number | null;
  createdAt: string;
}

export interface SessionTurnTelemetry {
  latestReview: SessionTurnReview | null;
}

interface LoopInboxItemSummaryRaw {
  card_id: number;
  session_id: string;
  title: string;
  project: string | null;
  machine: string | null;
  provider: string | null;
  execution_home: string | null;
  home_label: string | null;
  loop_mode: string;
  decision: string;
  execution_state: string | null;
  summary: string;
  recommended_action: string | null;
  follow_up_prompt: string | null;
  blocked_reasons: string[] | null;
  last_turn_at: string;
  card_state: string | null;
  card_state_reason: string | null;
  superseded_by_card_id: number | null;
  requires_attention: boolean;
}

interface LoopActionCardRaw extends LoopInboxItemSummaryRaw {
  rationale: string | null;
  mode_capability: string | null;
  mode_summary: string | null;
  last_user_text: string | null;
  last_assistant_text: string | null;
  available_actions: string[] | null;
}

interface LoopInboxActionResultRaw {
  session_id: string;
  review_id: number;
  action: string;
  status: string;
  reason: string | null;
  queued_job_id: number | null;
}

export type LoopInboxAction = "approve_recommended_action" | "reply_to_session" | "not_now";

export interface LoopInboxItem {
  cardId: number;
  sessionId: string;
  title: string;
  project: string | null;
  machine: string | null;
  provider: string | null;
  executionHome: string | null;
  homeLabel: string | null;
  loopMode: SessionLoopMode;
  decision: SessionTurnDecision;
  executionState: SessionTurnExecutionState;
  summary: string;
  recommendedAction: string | null;
  followUpPrompt: string | null;
  blockedReasons: string[];
  lastTurnAt: string;
  cardState: LoopCardState;
  cardStateReason: string | null;
  supersededByCardId: number | null;
  requiresAttention: boolean;
}

export interface LoopActionCard extends LoopInboxItem {
  rationale: string;
  modeCapability: SessionLoopModeCapability;
  modeSummary: string;
  lastUserText: string | null;
  lastAssistantText: string | null;
  availableActions: LoopInboxAction[];
}

export interface LoopInboxActionResult {
  sessionId: string;
  reviewId: number;
  action: LoopInboxAction;
  status: string;
  reason: string | null;
  queuedJobId: number | null;
}

export interface LoopInboxSnapshotEvent {
  items: LoopInboxItem[];
}

export interface LoopInboxStreamHandlers {
  onConnected?: () => void;
  onHeartbeat?: (timestamp: string) => void;
  onSnapshot?: (event: LoopInboxSnapshotEvent) => void;
  onError?: (error: Event) => void;
}

interface LoopPushConfigRaw {
  enabled: boolean;
  vapid_public_key: string | null;
}

export interface LoopPushConfig {
  enabled: boolean;
  vapidPublicKey: string | null;
}

export interface LoopPushSubscriptionRegistration {
  subscription: PushSubscriptionJSON;
  installId?: string | null;
  userAgent?: string | null;
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
    followUpPrompt: asString(row.follow_up_prompt),
    blockedReasons: asStringArray(row.blocked_reasons),
    status: asString(row.status) ?? "recorded",
    reason: asString(row.reason),
    runId: asNumber(row.run_id),
    actualOutcome: (asString(row.actual_outcome) ?? null) as SessionTurnOutcome | null,
    alignment: (asString(row.shadow_alignment) ?? null) as SessionTurnAlignment | null,
    assistantTurnFinishedAt: asString(row.assistant_turn_finished_at),
    turnLoopEnqueuedAt: asString(row.turn_loop_enqueued_at),
    turnLoopCompletedAt: asString(row.turn_loop_completed_at),
    queueLatencyMs: asNumber(row.queue_latency_ms),
    reviewLatencyMs: asNumber(row.review_latency_ms),
    processingLatencyMs: asNumber(row.processing_latency_ms),
    createdAt: row.created_at,
  };
}

function parseLoopInboxItem(row: LoopInboxItemSummaryRaw): LoopInboxItem {
  return {
    cardId: row.card_id,
    sessionId: row.session_id,
    title: asString(row.title) ?? "Untitled session",
    project: asString(row.project),
    machine: asString(row.machine),
    provider: asString(row.provider),
    executionHome: asString(row.execution_home),
    homeLabel: asString(row.home_label),
    loopMode: (asString(row.loop_mode) ?? "manual") as SessionLoopMode,
    decision: (asString(row.decision) ?? "done") as SessionTurnDecision,
    executionState: (asString(row.execution_state) ?? "no_action") as SessionTurnExecutionState,
    summary: asString(row.summary) ?? "No action needed.",
    recommendedAction: asString(row.recommended_action),
    followUpPrompt: asString(row.follow_up_prompt),
    blockedReasons: asStringArray(row.blocked_reasons),
    lastTurnAt: row.last_turn_at,
    cardState: (asString(row.card_state) ?? "active") as LoopCardState,
    cardStateReason: asString(row.card_state_reason),
    supersededByCardId: asNumber(row.superseded_by_card_id),
    requiresAttention: Boolean(row.requires_attention),
  };
}

function parseLoopActionCard(row: LoopActionCardRaw): LoopActionCard {
  return {
    ...parseLoopInboxItem(row),
    rationale: asString(row.rationale) ?? "",
    modeCapability: (asString(row.mode_capability) ?? "observe_only") as SessionLoopModeCapability,
    modeSummary: asString(row.mode_summary) ?? "",
    lastUserText: asString(row.last_user_text),
    lastAssistantText: asString(row.last_assistant_text),
    availableActions: asStringArray(row.available_actions) as LoopInboxAction[],
  };
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
  };
}

export async function fetchLoopInbox(): Promise<LoopInboxItem[]> {
  const rows = await request<LoopInboxItemSummaryRaw[]>("/oikos/loop-inbox");
  return rows.map(parseLoopInboxItem);
}

function parseStreamEventData<T>(event: MessageEvent): T | null {
  try {
    return JSON.parse(event.data) as T;
  } catch {
    return null;
  }
}

export function connectLoopInboxStream(handlers: LoopInboxStreamHandlers = {}): () => void {
  const eventSource = new EventSource("/api/oikos/loop-inbox/stream", { withCredentials: true });

  eventSource.addEventListener("connected", () => {
    handlers.onConnected?.();
  });

  eventSource.addEventListener("heartbeat", (event: MessageEvent) => {
    const data = parseStreamEventData<{ timestamp: string }>(event);
    if (data?.timestamp) {
      handlers.onHeartbeat?.(data.timestamp);
    }
  });

  eventSource.addEventListener("inbox_snapshot", (event: MessageEvent) => {
    const data = parseStreamEventData<{ items: LoopInboxItemSummaryRaw[] }>(event);
    if (!data?.items) {
      return;
    }
    handlers.onSnapshot?.({ items: data.items.map(parseLoopInboxItem) });
  });

  eventSource.onerror = (error) => {
    handlers.onError?.(error);
  };

  return () => {
    eventSource.close();
  };
}

export async function fetchLoopActionCard(cardId: number | string): Promise<LoopActionCard> {
  const row = await request<LoopActionCardRaw>(`/oikos/loop-inbox/cards/${cardId}`);
  return parseLoopActionCard(row);
}

export async function fetchLoopActionCardForSession(sessionId: string): Promise<LoopActionCard> {
  const row = await request<LoopActionCardRaw>(`/oikos/loop-inbox/${sessionId}`);
  return parseLoopActionCard(row);
}

export async function applyLoopInboxAction(
  cardId: number,
  action: LoopInboxAction,
  options?: { replyText?: string | null },
): Promise<LoopInboxActionResult> {
  const row = await request<LoopInboxActionResultRaw>(`/oikos/loop-inbox/cards/${cardId}/actions`, {
    method: "POST",
    body: JSON.stringify({
      action,
      reply_text: options?.replyText ?? null,
    }),
  });
  return {
    sessionId: row.session_id,
    reviewId: row.review_id,
    action: row.action as LoopInboxAction,
    status: asString(row.status) ?? "acted",
    reason: asString(row.reason),
    queuedJobId: asNumber(row.queued_job_id),
  };
}

export async function fetchLoopPushConfig(): Promise<LoopPushConfig> {
  const row = await request<LoopPushConfigRaw>("/oikos/push-config");
  return {
    enabled: Boolean(row.enabled),
    vapidPublicKey: asString(row.vapid_public_key),
  };
}

export async function registerLoopPushSubscription(
  payload: LoopPushSubscriptionRegistration,
): Promise<void> {
  await request("/oikos/push-subscriptions", {
    method: "POST",
    body: JSON.stringify({
      subscription: payload.subscription,
      install_id: payload.installId ?? null,
      user_agent: payload.userAgent ?? null,
    }),
  });
}

export async function deleteLoopPushSubscription(endpoint: string): Promise<void> {
  await request("/oikos/push-subscriptions", {
    method: "DELETE",
    body: JSON.stringify({ endpoint }),
  });
}
