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
  assistantUsage?: {
    prompt_tokens?: number | null;
    completion_tokens?: number | null;
    total_tokens?: number | null;
    reasoning_tokens?: number | null;
  };
  latencyMs?: number;
  assistantResponse?: string;
  responseTime?: number;
}
