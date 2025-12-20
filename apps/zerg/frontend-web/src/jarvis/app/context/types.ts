/**
 * Application state types for Jarvis PWA
 */

import type { RealtimeSession } from '@openai/agents/realtime'
import type { SessionManager } from '../../core'

/**
 * Conversation in sidebar
 */
export interface Conversation {
  id: string
  name: string
  meta: string
  active?: boolean
}

/**
 * Voice mode
 */
export type VoiceMode = 'push-to-talk' | 'hands-free'

/**
 * Voice status
 */
export type VoiceStatus = 'idle' | 'connecting' | 'ready' | 'listening' | 'processing' | 'speaking' | 'error'

/**
 * Chat message
 */
export type AssistantStatus = 'queued' | 'typing' | 'streaming' | 'final' | 'error' | 'canceled';

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp?: Date
  isStreaming?: boolean
  skipAnimation?: boolean
  /** OpenAI item ID for matching transcripts to placeholders */
  itemId?: string
  /** New fields for Option C (Jarvis Typing Indicator) */
  status?: AssistantStatus
  correlationId?: string
}

/**
 * Model info from bootstrap
 */
export interface ModelInfo {
  id: string
  display_name: string
  description: string
}

/**
 * User preferences for chat
 */
export interface ChatPreferences {
  chat_model: string
  reasoning_effort: 'none' | 'low' | 'medium' | 'high'
}

/**
 * Global application state
 */
export interface AppState {
  // Core OpenAI SDK objects
  agent: unknown | null
  session: RealtimeSession | null
  sessionManager: SessionManager | null

  // Conversation state
  messages: ChatMessage[]
  streamingContent: string
  userTranscriptPreview: string  // Live voice transcript preview
  currentConversationId: string | null
  conversations: Conversation[]

  // Voice state
  voiceMode: VoiceMode
  voiceStatus: VoiceStatus

  // UI state
  sidebarOpen: boolean
  isConnected: boolean

  // Jarvis-Zerg integration
  jarvisClient: unknown | null
  cachedAgents: unknown[]

  // Media state
  sharedMicStream: MediaStream | null

  // Chat preferences (model selection, reasoning effort)
  availableModels: ModelInfo[]
  preferences: ChatPreferences
}

/**
 * Actions for state updates
 */
export type AppAction =
  | { type: 'SET_SESSION'; session: RealtimeSession | null }
  | { type: 'SET_AGENT'; agent: unknown | null }
  | { type: 'SET_SESSION_MANAGER'; sessionManager: SessionManager | null }
  | { type: 'SET_MESSAGES'; messages: ChatMessage[] }
  | { type: 'ADD_MESSAGE'; message: ChatMessage }
  | { type: 'UPDATE_MESSAGE'; itemId: string; content: string }
  | { type: 'UPDATE_MESSAGE_BY_CORRELATION_ID'; correlationId: string; updates: Partial<ChatMessage> }
  | { type: 'SET_STREAMING_CONTENT'; content: string }
  | { type: 'SET_USER_TRANSCRIPT_PREVIEW'; text: string }
  | { type: 'SET_CONVERSATION_ID'; id: string | null }
  | { type: 'SET_CONVERSATIONS'; conversations: Conversation[] }
  | { type: 'SET_VOICE_MODE'; mode: VoiceMode }
  | { type: 'SET_VOICE_STATUS'; status: VoiceStatus }
  | { type: 'SET_SIDEBAR_OPEN'; open: boolean }
  | { type: 'SET_CONNECTED'; connected: boolean }
  | { type: 'SET_JARVIS_CLIENT'; client: unknown }
  | { type: 'SET_CACHED_AGENTS'; agents: unknown[] }
  | { type: 'SET_MIC_STREAM'; stream: MediaStream | null }
  | { type: 'SET_AVAILABLE_MODELS'; models: ModelInfo[] }
  | { type: 'SET_PREFERENCES'; preferences: ChatPreferences }
  | { type: 'UPDATE_PREFERENCE'; key: keyof ChatPreferences; value: string }
  | { type: 'RESET' }
