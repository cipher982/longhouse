// AUTO-GENERATED FILE - DO NOT EDIT
// Generated from sse-events.asyncapi.yml
// Using AsyncAPI 3.0 + SSE Protocol Code Generation
//
// This file contains strongly-typed SSE event definitions.
// To update, modify the schema file and run: python scripts/generate-sse-types.py schemas/sse-events.asyncapi.yml

// Event payload types

/** LLM token usage statistics */
export interface UsageData {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  /** Reasoning tokens (OpenAI o1/o3 models) */
  reasoning_tokens?: number;
}

/** Commis execution result */
export type CommisStatus = "success" | "failed";

export interface ConnectedPayload {
  /** Connection confirmation message */
  message: string;
  /** Course ID for this SSE stream */
  course_id: number;
  /** Optional client-provided correlation ID */
  client_correlation_id?: string;
}

export interface HeartbeatPayload {
  /** Optional heartbeat message */
  message?: string;
  /** ISO 8601 timestamp */
  timestamp?: string;
}

export interface ConciergeStartedPayload {
  /** Course ID (may be omitted in legacy events) */
  course_id?: number;
  /** Thread ID for this conversation */
  thread_id: number;
  /** User's task/question */
  task: string;
  /** Unique identifier for the assistant message (stable across tokens/completion) */
  message_id: string;
  /** For continuation courses, the message_id of the original course's message */
  continuation_of_message_id?: string;
  /** End-to-end trace ID for debugging (copy from UI for fiche debugging) */
  trace_id?: string;
}

export interface ConciergeThinkingPayload {
  /** Thinking status message */
  message: string;
  course_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface ConciergeTokenPayload {
  /** LLM token (may be empty string) */
  token: string;
  course_id?: number;
  thread_id?: number;
  /** Unique identifier for the assistant message */
  message_id?: string;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface ConciergeCompletePayload {
  /** Final concierge result */
  result: string;
  /** Completion status ('success' for normal completion, 'cancelled' for user-initiated cancellation) */
  status: "success" | "cancelled";
  /** Execution duration in milliseconds */
  duration_ms?: number;
  usage?: UsageData;
  course_id?: number;
  fiche_id?: number;
  thread_id?: number;
  /** URL for debug/inspection */
  debug_url?: string;
  /** Unique identifier for the assistant message */
  message_id?: string;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface ConciergeDeferredPayload {
  /** Deferred status message */
  message: string;
  /** URL to re-attach to the running course execution */
  attach_url?: string;
  /** Timeout that triggered deferral */
  timeout_seconds?: number;
  course_id?: number;
  fiche_id?: number;
  thread_id?: number;
  /** Unique identifier for the assistant message */
  message_id?: string;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface ConciergeWaitingPayload {
  /** Waiting status message (e.g., commis spawned) */
  message: string;
  /** Commis job ID (if applicable) */
  job_id?: number;
  /** If false, keep SSE stream open while waiting */
  close_stream?: boolean;
  course_id?: number;
  fiche_id?: number;
  thread_id?: number;
  /** Unique identifier for the assistant message */
  message_id?: string;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface ConciergeResumedPayload {
  course_id?: number;
  fiche_id?: number;
  thread_id: number;
  /** Unique identifier for the assistant message */
  message_id: string;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface ErrorPayload {
  /** Error message */
  error?: string;
  /** Alternative error message field */
  message?: string;
  course_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface CommisSpawnedPayload {
  /** Commis job ID */
  job_id: number;
  /** Tool call ID for the spawn_commis invocation */
  tool_call_id?: string;
  /** Commis task (may be truncated to 100 chars) */
  task: string;
  /** LLM model for commis */
  model?: string;
  course_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface CommisStartedPayload {
  job_id: number;
  /** Commis execution ID */
  commis_id: string;
  course_id?: number;
  /** Commis task (may be truncated) */
  task?: string;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface CommisCompletePayload {
  job_id: number;
  /** Commis execution ID */
  commis_id?: string;
  status: CommisStatus;
  duration_ms?: number;
  /** Error message (only present if status=failed) */
  error?: string;
  course_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface CommisSummaryReadyPayload {
  job_id: number;
  /** Commis execution ID */
  commis_id?: string;
  /** Extracted commis summary */
  summary: string;
  course_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface CommisToolStartedPayload {
  commis_id: string;
  tool_name: string;
  /** LangChain tool call ID */
  tool_call_id: string;
  /** Preview of tool arguments (may be truncated) */
  tool_args_preview?: string;
  /** Required for security (prevents cross-course leakage) */
  course_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface CommisToolCompletedPayload {
  commis_id: string;
  tool_name: string;
  tool_call_id: string;
  duration_ms: number;
  /** Preview of tool result (may be truncated) */
  result_preview?: string;
  course_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface CommisToolFailedPayload {
  commis_id: string;
  tool_name: string;
  tool_call_id: string;
  duration_ms: number;
  /** Error message */
  error: string;
  course_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface ConciergeToolStartedPayload {
  tool_name: string;
  /** Stable ID linking all events for this tool call */
  tool_call_id: string;
  /** Preview of tool arguments (may be truncated) */
  tool_args_preview?: string;
  /** Full tool arguments (for persistence/raw view) */
  tool_args?: Record<string, any>;
  /** Concierge course ID for correlation */
  course_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface ConciergeToolProgressPayload {
  tool_call_id: string;
  /** Progress message (log line) */
  message: string;
  /** Log level for styling */
  level?: "debug" | "info" | "warn" | "error";
  /** Optional progress percentage */
  progress_pct?: number;
  /** Optional structured data (metrics, artifacts preview) */
  data?: Record<string, any>;
  course_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface ConciergeToolCompletedPayload {
  tool_name: string;
  tool_call_id: string;
  duration_ms: number;
  /** Condensed result for collapsed view */
  result_preview?: string;
  /** Full result (for persistence/raw view) */
  result?: Record<string, any>;
  course_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface ConciergeToolFailedPayload {
  tool_name: string;
  tool_call_id: string;
  duration_ms: number;
  /** Error message */
  error: string;
  /** Full error details (stack trace, context) */
  error_details?: Record<string, any>;
  course_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

// All SSE event types as a constant array (use for validation)
export const SSE_EVENT_TYPES = [
  "connected",
  "heartbeat",
  "concierge_started",
  "concierge_thinking",
  "concierge_token",
  "concierge_complete",
  "concierge_deferred",
  "concierge_waiting",
  "concierge_resumed",
  "error",
  "commis_spawned",
  "commis_started",
  "commis_complete",
  "commis_summary_ready",
  "commis_tool_started",
  "commis_tool_completed",
  "commis_tool_failed",
  "concierge_tool_started",
  "concierge_tool_progress",
  "concierge_tool_completed",
  "concierge_tool_failed",
] as const;

// SSE event type union (derived from SSE_EVENT_TYPES)
export type SSEEventType = typeof SSE_EVENT_TYPES[number];

// SSE runtime types matching actual backend format
//
// IMPORTANT: The backend sends events in two formats:
// 1. Direct payload (connected, heartbeat): data is the payload JSON directly
// 2. Wrapped payload (all other events): data is { type, payload, client_correlation_id, timestamp }

/**
 * Wrapper format for most SSE events.
 * After JSON.parse(event.data), you get this structure for non-heartbeat events.
 */
export interface SSEEventWrapper<T> {
  type: SSEEventType;
  payload: T;
  client_correlation_id?: string;
  timestamp?: string;
}

/**
 * Type helper to extract the correct payload type for a given event.
 * Use with: const payload = (parsed as SSEEventWrapper<SupervisorStartedPayload>).payload;
 */
export type SSEPayloadFor<T extends SSEEventType> =
  T extends "connected" ? ConnectedPayload :
  T extends "heartbeat" ? HeartbeatPayload :
  T extends "concierge_started" ? ConciergeStartedPayload :
  T extends "concierge_thinking" ? ConciergeThinkingPayload :
  T extends "concierge_token" ? ConciergeTokenPayload :
  T extends "concierge_complete" ? ConciergeCompletePayload :
  T extends "concierge_deferred" ? ConciergeDeferredPayload :
  T extends "concierge_waiting" ? ConciergeWaitingPayload :
  T extends "concierge_resumed" ? ConciergeResumedPayload :
  T extends "error" ? ErrorPayload :
  T extends "commis_spawned" ? CommisSpawnedPayload :
  T extends "commis_started" ? CommisStartedPayload :
  T extends "commis_complete" ? CommisCompletePayload :
  T extends "commis_summary_ready" ? CommisSummaryReadyPayload :
  T extends "commis_tool_started" ? CommisToolStartedPayload :
  T extends "commis_tool_completed" ? CommisToolCompletedPayload :
  T extends "commis_tool_failed" ? CommisToolFailedPayload :
  T extends "concierge_tool_started" ? ConciergeToolStartedPayload :
  T extends "concierge_tool_progress" ? ConciergeToolProgressPayload :
  T extends "concierge_tool_completed" ? ConciergeToolCompletedPayload :
  T extends "concierge_tool_failed" ? ConciergeToolFailedPayload :
  never;

// Payload type lookup (for direct payload access after unwrapping)
export const SSE_DIRECT_PAYLOAD_EVENTS = ['connected', 'heartbeat'] as const;
export type SSEDirectPayloadEvent = typeof SSE_DIRECT_PAYLOAD_EVENTS[number];
