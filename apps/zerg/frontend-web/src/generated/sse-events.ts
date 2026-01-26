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

/** Worker execution result */
export type WorkerStatus = "success" | "failed";

export interface ConnectedPayload {
  /** Connection confirmation message */
  message: string;
  /** Run ID for this SSE stream */
  run_id: number;
  /** Optional client-provided correlation ID */
  client_correlation_id?: string;
}

export interface HeartbeatPayload {
  /** Optional heartbeat message */
  message?: string;
  /** ISO 8601 timestamp */
  timestamp?: string;
}

export interface SupervisorStartedPayload {
  /** Run ID (may be omitted in legacy events) */
  run_id?: number;
  /** Thread ID for this conversation */
  thread_id: number;
  /** User's task/question */
  task: string;
  /** Unique identifier for the assistant message (stable across tokens/completion) */
  message_id: string;
  /** For continuation runs, the message_id of the original run's message */
  continuation_of_message_id?: string;
  /** End-to-end trace ID for debugging (copy from UI for agent debugging) */
  trace_id?: string;
}

export interface SupervisorThinkingPayload {
  /** Thinking status message */
  message: string;
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface SupervisorTokenPayload {
  /** LLM token (may be empty string) */
  token: string;
  run_id?: number;
  thread_id?: number;
  /** Unique identifier for the assistant message */
  message_id?: string;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface SupervisorCompletePayload {
  /** Final supervisor result */
  result: string;
  /** Completion status ('success' for normal completion, 'cancelled' for user-initiated cancellation) */
  status: "success" | "cancelled";
  /** Execution duration in milliseconds */
  duration_ms?: number;
  usage?: UsageData;
  run_id?: number;
  agent_id?: number;
  thread_id?: number;
  /** URL for debug/inspection */
  debug_url?: string;
  /** Unique identifier for the assistant message */
  message_id?: string;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface SupervisorDeferredPayload {
  /** Deferred status message */
  message: string;
  /** URL to re-attach to the running execution */
  attach_url?: string;
  /** Timeout that triggered deferral */
  timeout_seconds?: number;
  run_id?: number;
  agent_id?: number;
  thread_id?: number;
  /** Unique identifier for the assistant message */
  message_id?: string;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface SupervisorWaitingPayload {
  /** Waiting status message (e.g., worker spawned) */
  message: string;
  /** Worker job ID (if applicable) */
  job_id?: number;
  /** If false, keep SSE stream open while waiting */
  close_stream?: boolean;
  run_id?: number;
  agent_id?: number;
  thread_id?: number;
  /** Unique identifier for the assistant message */
  message_id?: string;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface SupervisorResumedPayload {
  run_id?: number;
  agent_id?: number;
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
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface WorkerSpawnedPayload {
  /** Worker job ID */
  job_id: number;
  /** Tool call ID for the spawn_worker invocation */
  tool_call_id?: string;
  /** Worker task (may be truncated to 100 chars) */
  task: string;
  /** LLM model for worker */
  model?: string;
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface WorkerStartedPayload {
  job_id: number;
  /** Worker execution ID */
  worker_id: string;
  run_id?: number;
  /** Worker task (may be truncated) */
  task?: string;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface WorkerCompletePayload {
  job_id: number;
  /** Worker execution ID */
  worker_id?: string;
  status: WorkerStatus;
  duration_ms?: number;
  /** Error message (only present if status=failed) */
  error?: string;
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface WorkerSummaryReadyPayload {
  job_id: number;
  /** Worker execution ID */
  worker_id?: string;
  /** Extracted worker summary */
  summary: string;
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface WorkerToolStartedPayload {
  worker_id: string;
  tool_name: string;
  /** LangChain tool call ID */
  tool_call_id: string;
  /** Preview of tool arguments (may be truncated) */
  tool_args_preview?: string;
  /** Required for security (prevents cross-run leakage) */
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface WorkerToolCompletedPayload {
  worker_id: string;
  tool_name: string;
  tool_call_id: string;
  duration_ms: number;
  /** Preview of tool result (may be truncated) */
  result_preview?: string;
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface WorkerToolFailedPayload {
  worker_id: string;
  tool_name: string;
  tool_call_id: string;
  duration_ms: number;
  /** Error message */
  error: string;
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface WorkerOutputChunkPayload {
  /** Worker job ID (spawn_worker job) */
  job_id?: number;
  worker_id: string;
  /** Runner exec job UUID */
  runner_job_id?: string;
  /** Output stream for this chunk */
  stream: "stdout" | "stderr";
  /** Output chunk (may be truncated) */
  data: string;
  /** Required for security (prevents cross-run leakage) */
  run_id: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface SupervisorToolStartedPayload {
  tool_name: string;
  /** Stable ID linking all events for this tool call */
  tool_call_id: string;
  /** Preview of tool arguments (may be truncated) */
  tool_args_preview?: string;
  /** Full tool arguments (for persistence/raw view) */
  tool_args?: Record<string, any>;
  /** Supervisor run ID for correlation */
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface SupervisorToolProgressPayload {
  tool_call_id: string;
  /** Progress message (log line) */
  message: string;
  /** Log level for styling */
  level?: "debug" | "info" | "warn" | "error";
  /** Optional progress percentage */
  progress_pct?: number;
  /** Optional structured data (metrics, artifacts preview) */
  data?: Record<string, any>;
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface SupervisorToolCompletedPayload {
  tool_name: string;
  tool_call_id: string;
  duration_ms: number;
  /** Condensed result for collapsed view */
  result_preview?: string;
  /** Full result (for persistence/raw view) */
  result?: Record<string, any>;
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

export interface SupervisorToolFailedPayload {
  tool_name: string;
  tool_call_id: string;
  duration_ms: number;
  /** Error message */
  error: string;
  /** Full error details (stack trace, context) */
  error_details?: Record<string, any>;
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
}

/** Trigger session picker modal in frontend */
export interface ShowSessionPickerPayload {
  /** Current supervisor run ID */
  run_id?: number;
  /** End-to-end trace ID for debugging */
  trace_id?: string;
  /** Optional filters to pre-populate the picker */
  filters?: Record<string, any>;
}

// All SSE event types as a constant array (use for validation)
export const SSE_EVENT_TYPES = [
  "connected",
  "heartbeat",
  "supervisor_started",
  "supervisor_thinking",
  "supervisor_token",
  "supervisor_complete",
  "supervisor_deferred",
  "supervisor_waiting",
  "supervisor_resumed",
  "error",
  "worker_spawned",
  "worker_started",
  "worker_complete",
  "worker_summary_ready",
  "worker_tool_started",
  "worker_tool_completed",
  "worker_tool_failed",
  "worker_output_chunk",
  "supervisor_tool_started",
  "supervisor_tool_progress",
  "supervisor_tool_completed",
  "supervisor_tool_failed",
  "show_session_picker",
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
  T extends "supervisor_started" ? SupervisorStartedPayload :
  T extends "supervisor_thinking" ? SupervisorThinkingPayload :
  T extends "supervisor_token" ? SupervisorTokenPayload :
  T extends "supervisor_complete" ? SupervisorCompletePayload :
  T extends "supervisor_deferred" ? SupervisorDeferredPayload :
  T extends "supervisor_waiting" ? SupervisorWaitingPayload :
  T extends "supervisor_resumed" ? SupervisorResumedPayload :
  T extends "error" ? ErrorPayload :
  T extends "worker_spawned" ? WorkerSpawnedPayload :
  T extends "worker_started" ? WorkerStartedPayload :
  T extends "worker_complete" ? WorkerCompletePayload :
  T extends "worker_summary_ready" ? WorkerSummaryReadyPayload :
  T extends "worker_tool_started" ? WorkerToolStartedPayload :
  T extends "worker_tool_completed" ? WorkerToolCompletedPayload :
  T extends "worker_tool_failed" ? WorkerToolFailedPayload :
  T extends "worker_output_chunk" ? WorkerOutputChunkPayload :
  T extends "supervisor_tool_started" ? SupervisorToolStartedPayload :
  T extends "supervisor_tool_progress" ? SupervisorToolProgressPayload :
  T extends "supervisor_tool_completed" ? SupervisorToolCompletedPayload :
  T extends "supervisor_tool_failed" ? SupervisorToolFailedPayload :
  T extends "show_session_picker" ? ShowSessionPickerPayload :
  never;

// Payload type lookup (for direct payload access after unwrapping)
export const SSE_DIRECT_PAYLOAD_EVENTS = ['connected', 'heartbeat'] as const;
export type SSEDirectPayloadEvent = typeof SSE_DIRECT_PAYLOAD_EVENTS[number];
