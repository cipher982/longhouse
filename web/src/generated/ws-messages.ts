// AUTO-GENERATED FILE - DO NOT EDIT
// Generated from ws-protocol-asyncapi.yml
// Using AsyncAPI 3.0 + TypeScript Code Generation
//
// This file contains strongly-typed WebSocket message definitions.
// To update, modify the schema file and run: python scripts/generate/generate-ws-types-modern.py

// Base envelope structure for all WebSocket messages
export interface Envelope<T = unknown> {
  /** Protocol version */
  v: number;
  /** Message type identifier */
  type: string;
  /** Topic routing string (e.g., 'session:123', 'thread:456') */
  topic: string;
  /** Optional request correlation ID */
  req_id?: string;
  /** Timestamp in milliseconds since epoch */
  ts: number;
  /** Message payload - structure depends on type */
  data: T;
}

// Message payload types

export interface UserRef {
  id: number;
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

export interface UserUpdateData {
  id: number;
  email?: string;
  display_name?: string;
  avatar_url?: string;
}

export interface OpsEventData {
  type: "notice";
  message?: string;
  status?: string;
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

/** User profile update */
export interface UserUpdate extends Envelope<UserUpdateData> {
  type: 'user_update';
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
  | UserUpdate
  | OpsEvent
