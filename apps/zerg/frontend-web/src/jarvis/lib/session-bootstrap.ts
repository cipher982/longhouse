/**
 * Session Bootstrap
 * Single source of truth for session initialization.
 * Loads data once and provides to all consumers.
 *
 * This module solves the SSOT violation where UI and Realtime
 * previously fetched history separately, causing divergence.
 *
 * ARCHITECTURE:
 * Server history (Concierge/Postgres) ──single query──> Bootstrap Result ──> UI (full history)
 *                                                                    ──> Realtime (trimmed + mapped)
 */

import { logger } from '../core'
import type { ConversationTurn } from '../data'
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
  conversationId?: string | null
  history: ConversationTurn[]
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
  const { realtimeHistoryTurns = 8 } = options

  const conversationId = options.conversationId ?? null
  const fullHistory = Array.isArray(options.history) ? options.history : []
  logger.info(`📚 Bootstrap: using ${fullHistory.length} turns from server history`)

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
