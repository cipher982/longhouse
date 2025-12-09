/**
 * Session Bootstrap
 * Single source of truth for session initialization.
 * Loads data once and provides to all consumers.
 *
 * This module solves the SSOT violation where UI and Realtime
 * previously fetched history separately, causing divergence.
 *
 * ARCHITECTURE:
 * IndexedDB ──single query──> Bootstrap Result ──> UI (full history)
 *                                              ──> Realtime (trimmed + mapped)
 */

import { logger, type SessionManager } from '@jarvis/core'
import type { ConversationTurn } from '@jarvis/data-local'
import { sessionHandler } from './session-handler'
import { mapConversationToRealtimeItems, trimForRealtime } from './history-mapper'
import type { VoiceAgentConfig } from '../contexts/types'

export interface BootstrapResult {
  session: any // RealtimeSession
  agent: any // RealtimeAgent
  conversationId: string | null
  history: ConversationTurn[]
  hydratedItemCount: number
}

export interface BootstrapOptions {
  context: VoiceAgentConfig
  sessionManager: SessionManager
  mediaStream?: MediaStream
  audioElement?: HTMLAudioElement
  tools?: any[]
  onTokenRequest: () => Promise<string>
  realtimeHistoryTurns?: number
}

/**
 * Bootstrap a session with SSOT history.
 * Returns the same history data for both UI and Realtime hydration.
 *
 * GUARANTEES:
 * 1. History is loaded exactly once from IndexedDB
 * 2. UI receives full history for display
 * 3. Realtime receives trimmed/mapped subset from same data
 * 4. No divergence possible between UI and model context
 */
export async function bootstrapSession(options: BootstrapOptions): Promise<BootstrapResult> {
  const { sessionManager, realtimeHistoryTurns = 8 } = options

  // 1. Get conversation ID (SSOT)
  const conversationId = await sessionManager.getConversationManager().getCurrentConversationId()

  // 2. Load history ONCE (SSOT) - this is the critical single query
  const fullHistory = await sessionManager.getConversationHistory()
  logger.info(`📚 Bootstrap: loaded ${fullHistory.length} turns from IndexedDB`)

  // 3. Prepare history for Realtime (subset of same data)
  const realtimeHistory = trimForRealtime(fullHistory, realtimeHistoryTurns)
  const realtimeItems = mapConversationToRealtimeItems(realtimeHistory)

  // 4. Connect session with explicit history (not re-queried)
  const { session, agent } = await sessionHandler.connectWithHistory({
    context: options.context,
    mediaStream: options.mediaStream,
    audioElement: options.audioElement,
    tools: options.tools,
    onTokenRequest: options.onTokenRequest,
    historyItems: realtimeItems, // Pass explicitly, don't re-query
  })

  logger.info(`📜 Bootstrap: hydrated ${realtimeItems.length} items into Realtime`)

  return {
    session,
    agent,
    conversationId,
    history: fullHistory, // Full history for UI
    hydratedItemCount: realtimeItems.length,
  }
}
