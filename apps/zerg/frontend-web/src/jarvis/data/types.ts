/**
 * Shared type definitions for the Voice AI application
 */

export interface ConversationTurn {
  id: string;
  timestamp: Date;
  userAudio?: ArrayBuffer;
  userTranscript?: string;
  assistantText?: string;
  assistantAudio?: ArrayBuffer;
  latencyMs?: number;
  conversationId?: string;
  assistantResponse?: string;
  responseTime?: number;
}

export type TurnRole = 'user' | 'assistant' | 'tool';

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: Date;
  isStreaming?: boolean;
  // Internal tie-breaker for identical timestamps (assigned by renderer)
  seq?: number;
}

export type OutboxOpType =
  | 'append_turn'
  | 'update_conversation_meta'
  | 'delete_conversation'
  | 'delete_turn'
  | 'add_doc'
  | 'update_doc'
  | 'delete_doc'
  | 'set_kv';

export interface OutboxOp {
  opId: string;
  deviceId: string;
  type: OutboxOpType;
  body: unknown; // Narrow per-op when needed
  lamport: number;
  ts: Date;
}

export interface CompanyDocument {
  id: string;
  content: string;
  embedding: number[];
  metadata: {
    source: string;
    type: 'financial' | 'product' | 'policy' | 'organizational' | 'strategic';
    lastUpdated: string;
    category?: string;
    quarter?: string;
    product?: string;
    department?: string;
  };
}

export interface SearchResult {
  document: CompanyDocument;
  score: number;
}
