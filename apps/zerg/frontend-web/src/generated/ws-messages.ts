// AUTO-GENERATED FILE - DO NOT EDIT
// Generated from ws-protocol-asyncapi.yml
// Using AsyncAPI 3.0 + TypeScript Code Generation
//
// This file contains strongly-typed WebSocket message definitions.
// To update, modify the schema file and run: python scripts/generate-ws-types-modern.py

// Base envelope structure for all WebSocket messages
export interface Envelope<T = unknown> {
  /** Protocol version */
  v: number;
  /** Message type identifier */
  type: string;
  /** Topic routing string (e.g., 'agent:123', 'thread:456') */
  topic: string;
  /** Optional request correlation ID */
  req_id?: string;
  /** Timestamp in milliseconds since epoch */
  ts: number;
  /** Message payload - structure depends on type */
  data: T;
}

// Message payload types

export interface AgentRef {
  id: number;
}

export interface ThreadRef {
  thread_id: number;
}

export interface UserRef {
  id: number;
}

export interface ExecutionRef {
  execution_id: number;
}

export interface PingData {
  timestamp?: number;
}

export interface PongData {
  timestamp?: number;
}

export interface ErrorData {
  error: string;
  details?: Record<string, any>;
}

export interface SubscribeData {
  topics: string[];
  message_id?: string;
}

export interface SubscribeAckData {
  /** Correlation ID matching the original subscribe request */
  message_id: string;
  /** List of topics that were successfully subscribed */
  topics: string[];
}

export interface SubscribeErrorData {
  /** Correlation ID matching the original subscribe request */
  message_id: string;
  /** List of topics that failed to subscribe */
  topics?: string[];
  /** Human-readable error message */
  error: string;
  /** Machine-readable error code (e.g., NOT_FOUND, FORBIDDEN) */
  error_code?: string;
}

export interface UnsubscribeData {
  topics: string[];
  message_id?: string;
}

export interface SendMessageData {
  thread_id: number;
  content: string;
  metadata?: Record<string, any>;
}

export interface ThreadMessageData {
  thread_id: number;
  message: Record<string, any>;
}

export interface ThreadEventData {
  thread_id: number;
  agent_id?: number;
  title?: string;
  created_at?: string;
  updated_at?: string;
}

export interface StreamStartData {
  thread_id: number;
}

export interface StreamChunkData {
  thread_id: number;
  chunk_type: "assistant_token" | "assistant_message" | "tool_output";
  content?: string;
  tool_name?: string;
  tool_call_id?: string;
  message_id?: number;
}

export interface StreamEndData {
  thread_id: number;
}

export interface AssistantIdData {
  thread_id: number;
  message_id: number;
}

export interface AgentEventData {
  id: number;
  status?: string;
  last_run_at?: string;
  next_run_at?: string;
  last_error?: string;
  name?: string;
  description?: string;
}

export interface RunUpdateData {
  id: number;
  agent_id: number;
  thread_id?: number;
  status: "queued" | "running" | "success" | "failed";
  trigger?: "manual" | "schedule" | "chat" | "webhook" | "api";
  started_at?: string;
  finished_at?: string;
  duration_ms?: number;
  error?: string;
}

export interface UserUpdateData {
  id: number;
  email?: string;
  display_name?: string;
  avatar_url?: string;
}

export interface NodeStateData {
  execution_id: number;
  node_id: string;
  /** Current execution phase - what is happening NOW */
  phase: "waiting" | "running" | "finished";
  /** Execution result - how did it END (only when phase=finished) */
  result?: "success" | "failure" | "cancelled";
  /** Attempt number for retry tracking */
  attempt_no?: number;
  /** Classification of failure type (only when result=failure) */
  failure_kind?: "user" | "system" | "timeout" | "external" | "unknown";
  /** Detailed error message for failures */
  error_message?: string;
  output?: Record<string, any>;
}

export interface ExecutionFinishedData {
  execution_id: number;
  /** How the execution ended */
  result: "success" | "failure" | "cancelled";
  /** Final attempt number */
  attempt_no?: number;
  /** Classification of failure type (only when result=failure) */
  failure_kind?: "user" | "system" | "timeout" | "external" | "unknown";
  /** Detailed error message for failures */
  error_message?: string;
  /** Total execution time in milliseconds */
  duration_ms?: number;
}

export interface NodeLogData {
  execution_id: number;
  node_id: string;
  stream: "stdout" | "stderr";
  text: string;
}

export interface OpsEventData {
  type: "run_started" | "run_success" | "run_failed" | "agent_created" | "agent_updated" | "thread_message_created" | "budget_denied";
  agent_id?: number;
  run_id?: number;
  thread_id?: number;
  duration_ms?: number;
  error?: string;
  agent_name?: string;
  status?: string;
  scope?: "user" | "global";
  percent?: number;
  used_usd?: number;
  limit_cents?: number;
  user_email?: string;
}

// Typed message definitions with envelopes

/** Heartbeat ping from server */
export interface PingMessage extends Envelope<PingData> {
  type: 'ping';
}

/** Heartbeat response from client */
export interface PongMessage extends Envelope<PongData> {
  type: 'pong';
}

/** Protocol or application error */
export interface ErrorMessage extends Envelope<ErrorData> {
  type: 'error';
}

/** Subscribe to topic(s) */
export interface SubscribeMessage extends Envelope<SubscribeData> {
  type: 'subscribe';
}

/** Subscription confirmation (server to client) */
export interface SubscribeAckMessage extends Envelope<SubscribeAckData> {
  type: 'subscribe_ack';
}

/** Subscription failure notification (server to client) */
export interface SubscribeErrorMessage extends Envelope<SubscribeErrorData> {
  type: 'subscribe_error';
}

/** Unsubscribe from topic(s) */
export interface UnsubscribeMessage extends Envelope<UnsubscribeData> {
  type: 'unsubscribe';
}

/** Client request to send a message to a thread */
export interface SendMessageRequest extends Envelope<SendMessageData> {
  type: 'send_message';
}

/** New message added to thread */
export interface ThreadMessage extends Envelope<ThreadMessageData> {
  type: 'thread_message';
}

/** Thread lifecycle event */
export interface ThreadEvent extends Envelope<ThreadEventData> {
  type: 'thread_event';
}

/** Assistant response streaming started */
export interface StreamStart extends Envelope<StreamStartData> {
  type: 'stream_start';
}

/** Chunk of streaming assistant response */
export interface StreamChunk extends Envelope<StreamChunkData> {
  type: 'stream_chunk';
}

/** Assistant response streaming finished */
export interface StreamEnd extends Envelope<StreamEndData> {
  type: 'stream_end';
}

/** Assistant message ID assignment */
export interface AssistantId extends Envelope<AssistantIdData> {
  type: 'assistant_id';
}

/** Agent lifecycle or status event */
export interface AgentEvent extends Envelope<AgentEventData> {
  type: 'agent_event';
}

/** Agent run status update */
export interface RunUpdate extends Envelope<RunUpdateData> {
  type: 'run_update';
}

/** User profile update */
export interface UserUpdate extends Envelope<UserUpdateData> {
  type: 'user_update';
}

/** Workflow node state change */
export interface NodeState extends Envelope<NodeStateData> {
  type: 'node_state';
}

/** Workflow execution completed */
export interface ExecutionFinished extends Envelope<ExecutionFinishedData> {
  type: 'execution_finished';
}

/** Workflow node log output */
export interface NodeLog extends Envelope<NodeLogData> {
  type: 'node_log';
}

/** Normalized operational ticker event for admin dashboard */
export interface OpsEvent extends Envelope<OpsEventData> {
  type: 'ops_event';
}

// Discriminated union of all WebSocket messages
export type WebSocketMessage =
  | PingMessage
  | PongMessage
  | ErrorMessage
  | SubscribeMessage
  | SubscribeAckMessage
  | SubscribeErrorMessage
  | UnsubscribeMessage
  | SendMessageRequest
  | ThreadMessage
  | ThreadEvent
  | StreamStart
  | StreamChunk
  | StreamEnd
  | AssistantId
  | AgentEvent
  | RunUpdate
  | UserUpdate
  | NodeState
  | ExecutionFinished
  | NodeLog
  | OpsEvent
