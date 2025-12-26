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
}

export interface SupervisorThinkingPayload {
  /** Thinking status message */
  message: string;
  run_id?: number;
}

export interface SupervisorTokenPayload {
  /** LLM token (may be empty string) */
  token: string;
  run_id?: number;
  thread_id?: number;
}

export interface SupervisorCompletePayload {
  /** Final supervisor result */
  result: string;
  /** Completion status (always 'success' for this event) */
  status: "success";
  /** Execution duration in milliseconds */
  duration_ms?: number;
  usage?: UsageData;
  run_id?: number;
  agent_id?: number;
  thread_id?: number;
  /** URL for debug/inspection */
  debug_url?: string;
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
}

export interface ErrorPayload {
  /** Error message */
  error?: string;
  /** Alternative error message field */
  message?: string;
  run_id?: number;
}

export interface WorkerSpawnedPayload {
  /** Worker job ID */
  job_id: number;
  /** Worker task (may be truncated to 100 chars) */
  task: string;
  /** LLM model for worker */
  model?: string;
  run_id?: number;
}

export interface WorkerStartedPayload {
  job_id: number;
  /** Worker execution ID */
  worker_id: string;
  run_id?: number;
  /** Worker task (may be truncated) */
  task?: string;
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
}

export interface WorkerSummaryReadyPayload {
  job_id: number;
  /** Worker execution ID */
  worker_id?: string;
  /** Extracted worker summary */
  summary: string;
  run_id?: number;
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
}

export interface WorkerToolCompletedPayload {
  worker_id: string;
  tool_name: string;
  tool_call_id: string;
  duration_ms: number;
  /** Preview of tool result (may be truncated) */
  result_preview?: string;
  run_id?: number;
}

export interface WorkerToolFailedPayload {
  worker_id: string;
  tool_name: string;
  tool_call_id: string;
  duration_ms: number;
  /** Error message */
  error: string;
  run_id?: number;
}

// SSE event type union
export type SSEEventType =
  "connected"
  | "heartbeat"
  | "supervisor_started"
  | "supervisor_thinking"
  | "supervisor_token"
  | "supervisor_complete"
  | "supervisor_deferred"
  | "error"
  | "worker_spawned"
  | "worker_started"
  | "worker_complete"
  | "worker_summary_ready"
  | "worker_tool_started"
  | "worker_tool_completed"
  | "worker_tool_failed";

// SSE event discriminated union for type-safe event handling
export type SSEEventMap =
  { event: "connected"; data: ConnectedPayload; id?: number }
  | { event: "heartbeat"; data: HeartbeatPayload; id?: number }
  | { event: "supervisor_started"; data: SupervisorStartedPayload; id?: number }
  | { event: "supervisor_thinking"; data: SupervisorThinkingPayload; id?: number }
  | { event: "supervisor_token"; data: SupervisorTokenPayload; id?: number }
  | { event: "supervisor_complete"; data: SupervisorCompletePayload; id?: number }
  | { event: "supervisor_deferred"; data: SupervisorDeferredPayload; id?: number }
  | { event: "error"; data: ErrorPayload; id?: number }
  | { event: "worker_spawned"; data: WorkerSpawnedPayload; id?: number }
  | { event: "worker_started"; data: WorkerStartedPayload; id?: number }
  | { event: "worker_complete"; data: WorkerCompletePayload; id?: number }
  | { event: "worker_summary_ready"; data: WorkerSummaryReadyPayload; id?: number }
  | { event: "worker_tool_started"; data: WorkerToolStartedPayload; id?: number }
  | { event: "worker_tool_completed"; data: WorkerToolCompletedPayload; id?: number }
  | { event: "worker_tool_failed"; data: WorkerToolFailedPayload; id?: number };
