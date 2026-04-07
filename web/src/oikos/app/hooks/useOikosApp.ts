/**
 * useOikosApp - Unified Oikos Application Hook
 *
 * This hook manages the entire Oikos application lifecycle:
 * - Initialization (OikosClient, bootstrap, context, history)
 * - Voice connection (mic, session, voice controller)
 * - Text messaging (via OikosChatController)
 * - State synchronization (directly to React context)
 *
 * Replaces the old architecture:
 *   Controllers → global bridge → dispatch → context
 *
 * New architecture:
 *   Controllers → useOikosApp callbacks → dispatch → context
 */

import { useEffect, useCallback, useRef, useState } from 'react'
import { useAppDispatch, useAppState, type ChatMessage } from '../context'
import { logger, getOikosClient, type OikosAPIClient } from '../../core'
import { DEFAULT_CHAT_PREFERENCES } from '../../core/model-config'
import type { ConversationTurn } from '../../data'

// Import controllers (keep these - they're pure business logic)
import { voiceController, type VoiceEvent } from '../../lib/voice-controller'
import { audioController } from '../../lib/audio-controller'
import { feedbackSystem } from '../../lib/feedback-system'
import { OikosChatController, type AssistantMessageUpdate } from '../../lib/oikos-chat-controller'
import type { BootstrapResult } from '../../lib/session-bootstrap'
import { contextLoader } from '../../contexts/context-loader'
import { commisProgressStore } from '../../lib/commis-progress-store'
import { oikosToolStore, type OikosToolCall } from '../../lib/oikos-tool-store'
import type { VoiceAgentConfig } from '../../contexts/types'
import { getZergApiUrl, CONFIG, toAbsoluteUrl } from '../../lib/config'
import { uuid } from '../../lib/uuid'
import { bootstrapStore, type BootstrapData } from '../../lib/bootstrap-store'
import { eventBus } from '../../lib/event-bus'
import { timelineLogger } from '../../lib/timeline-logger'
import { fetchWithRefresh } from '../../../lib/auth-refresh'

const VOICE_INPUT_MODE: 'turn-based' | 'realtime' = 'turn-based'

async function loadRealtimeBootstrap() {
  return import('../../lib/session-bootstrap')
}

async function loadRealtimeSessionHandler() {
  return import('../../lib/session-handler')
}

interface OikosAppState {
  initialized: boolean
  connecting: boolean
  connected: boolean
  reconnecting: boolean
  voiceStatus: 'idle' | 'connecting' | 'ready' | 'listening' | 'processing' | 'speaking' | 'error'
  bootstrap: BootstrapData | null
  currentContext: VoiceAgentConfig | null
  oikosClient: OikosAPIClient | null
}

export type OikosHistoryView = 'surface' | 'all'

/**
 * Main hook for Oikos application
 */
export function useOikosApp() {
  const dispatch = useAppDispatch()
  const appState = useAppState()

  // Local state (not in React context - these are internal implementation details)
  const [state, setState] = useState<OikosAppState>({
    initialized: false,
    connecting: false,
    connected: false,
    reconnecting: false,
    voiceStatus: 'idle',
    bootstrap: null,
    currentContext: null,
    oikosClient: null,
  })

  // Refs for singleton instances
  const oikosChatRef = useRef<OikosChatController | null>(null)
  const lastBootstrapResultRef = useRef<BootstrapResult | null>(null)
  const lastOikosTurnsRef = useRef<ConversationTurn[]>([])
  const initStartedRef = useRef(false)
  const [historyView, setHistoryViewState] = useState<OikosHistoryView>('surface')
  // Track if messages were pre-hydrated (e.g., from OikosChatPage with ?thread= param)
  // If pre-hydrated, we should skip the initial history load to avoid state clobbering.
  const messagesPreHydratedRef = useRef(appState.messages.length > 0)

  // Helper to update internal state
  const updateState = useCallback((updates: Partial<OikosAppState>) => {
    setState(prev => ({ ...prev, ...updates }))
  }, [])

  // Update voice status in both local state and React context
  const setVoiceStatus = useCallback((status: OikosAppState['voiceStatus']) => {
    updateState({ voiceStatus: status })
    dispatch({ type: 'SET_VOICE_STATUS', status })
  }, [dispatch, updateState])

  const handleStreamingTextChange = useCallback((text: string) => {
    dispatch({ type: 'SET_STREAMING_CONTENT', content: text })
  }, [dispatch])

  const handleAssistantMessageUpdate = useCallback((messageId: string, updates: AssistantMessageUpdate) => {
    dispatch({
      type: 'UPDATE_MESSAGE_BY_MESSAGE_ID',
      messageId,
      updates,
    })
  }, [dispatch])

  const handleToast = useCallback((message: string, variant: 'success' | 'error' | 'info') => {
    logger.info(`[Toast] ${variant}: ${message}`)
  }, [])

  // ============= Initialization =============

  const initializeOikosClient = useCallback(async () => {
    try {
      const zergApiUrl = getZergApiUrl()
      logger.info(`[useOikosApp] Initializing OikosClient with URL: ${zergApiUrl}`)

      const oikosClient = getOikosClient(zergApiUrl)
      updateState({ oikosClient })

      const isAuthed = await oikosClient.isAuthenticated()
      if (isAuthed) {
        logger.info('[useOikosApp] OikosClient authenticated')
      } else {
        logger.warn('[useOikosApp] Not authenticated - log in to enable oikos features')
      }

      return oikosClient
    } catch (error) {
      logger.error('[useOikosApp] Failed to initialize OikosClient:', error)
      return null
    }
  }, [updateState])

  const fetchBootstrap = useCallback(async () => {
    try {
      logger.info('[useOikosApp] Fetching bootstrap configuration...')
      const response = await fetchWithRefresh(toAbsoluteUrl(`${CONFIG.OIKOS_API_BASE}/bootstrap`), {
        credentials: 'include',
      })

      if (!response.ok) {
        throw new Error(`Bootstrap fetch failed: ${response.status}`)
      }

      const bootstrap = await response.json() as BootstrapData
      updateState({ bootstrap })
      bootstrapStore.setBootstrap(bootstrap)

      // Update React context with bootstrap data
      if (bootstrap.available_models) {
        dispatch({ type: 'SET_AVAILABLE_MODELS', models: bootstrap.available_models })
      }
      if (bootstrap.preferences) {
        dispatch({ type: 'SET_PREFERENCES', preferences: bootstrap.preferences })
      }

      logger.info('[useOikosApp] Bootstrap configuration loaded')
      return bootstrap
    } catch (error) {
      logger.error('[useOikosApp] Failed to fetch bootstrap:', error)
      bootstrapStore.setBootstrap(null)
      return null
    }
  }, [dispatch, updateState])

  const initializeContext = useCallback(async () => {
    try {
      const contextName = await contextLoader.autoDetectContext()
      logger.info(`[useOikosApp] Loading context: ${contextName}`)

      const currentContext = await contextLoader.loadContext(contextName)
      updateState({ currentContext })

      if (messagesPreHydratedRef.current) {
        logger.info('[useOikosApp] Using pre-hydrated messages for initial chat view')
      }

      logger.info(`[useOikosApp] Context initialized: ${contextName}`)
      return currentContext
    } catch (error) {
      logger.error('[useOikosApp] Failed to initialize context:', error)
      throw error
    }
  }, [updateState])

  const loadOikosHistory = useCallback(async (options?: { view?: OikosHistoryView }) => {
    if (!oikosChatRef.current) return []

    // Skip loading if messages were pre-hydrated (e.g., from OikosChatPage with ?thread=)
    if (messagesPreHydratedRef.current) {
      logger.info('[useOikosApp] Skipping history load - messages were pre-hydrated')
      return []
    }

    try {
      logger.info('[useOikosApp] Loading Oikos chat history...')
      const view = options?.view || historyView
      const messages = await oikosChatRef.current.loadHistory(50, {
        surface_id: 'web',
        view,
      })

      if (messages.length > 0) {
        // Extract and hydrate tool calls from history
        const historicalTools: OikosToolCall[] = []
        for (const msg of messages) {
          if (msg.role === 'assistant' && msg.tool_calls) {
            for (const tc of msg.tool_calls) {
              // Convert API format to store format
              const tool: OikosToolCall = {
                toolCallId: tc.tool_call_id,
                toolName: tc.tool_name,
                status: 'completed', // Historical tools are always completed
                runId: 0, // Historical - no run association needed
                startedAt: msg.timestamp.getTime(),
                completedAt: msg.timestamp.getTime(),
                args: tc.args,
                resultPreview: tc.result?.substring(0, 200),
                result: tc.result ? { raw: tc.result } : undefined,
                logs: [],
              }

              // For spawn_commis, include commis metadata
              if (tc.tool_name === 'spawn_commis' && tc.commis) {
                const nestedTools = tc.commis.tools.map(wt => ({
                  toolCallId: `${tc.tool_call_id}-${wt.tool_name}`,
                  toolName: wt.tool_name,
                  status: wt.status as 'running' | 'completed' | 'failed',
                  durationMs: wt.duration_ms,
                  resultPreview: wt.result_preview,
                  error: wt.error,
                }))

                tool.result = {
                  commisStatus: tc.commis.status,
                  commisSummary: tc.commis.summary,
                  nestedTools,
                }
              }

              historicalTools.push(tool)
            }
          }
        }

        // Hydrate the oikos tool store with historical tools
        if (historicalTools.length > 0) {
          oikosToolStore.loadTools(historicalTools)
          logger.info(`[useOikosApp] Hydrated ${historicalTools.length} historical tool calls`)
        }

        // Note: Internal orchestration messages are now filtered server-side via the
        // `internal` column on ThreadMessage. The history API only returns user-facing messages.
        const history: ConversationTurn[] = []
        const chatMessages: ChatMessage[] = []
        for (const msg of messages) {
          const msgId = uuid()
          const timestamp = msg.timestamp ? new Date(msg.timestamp) : new Date()
          if (msg.role === 'user') {
            history.push({
              id: msgId,
              timestamp,
              userTranscript: msg.content,
            })
            chatMessages.push({
              id: msgId,
              role: 'user',
              content: msg.content,
              originSurfaceId: msg.origin_surface_id,
              deliverySurfaceId: msg.delivery_surface_id,
              visibility: msg.visibility,
              timestamp,
            })
            continue
          }
          if (msg.role === 'assistant') {
            history.push({
              id: msgId,
              timestamp,
              assistantResponse: msg.content,
              assistantUsage: msg.usage,
            })
            chatMessages.push({
              id: `${msgId}-asst`,
              role: 'assistant',
              content: msg.content,
              originSurfaceId: msg.origin_surface_id,
              deliverySurfaceId: msg.delivery_surface_id,
              visibility: msg.visibility,
              timestamp,
              usage: msg.usage,
            })
          }
        }

        lastOikosTurnsRef.current = history
        dispatch({ type: 'SET_MESSAGES', messages: chatMessages })
        logger.info(`[useOikosApp] Loaded ${messages.length} messages from history`)
        return history
      }

      lastOikosTurnsRef.current = []
      return []
    } catch (error) {
      logger.warn('[useOikosApp] Failed to load history (non-fatal):', error)
      return []
    }
  }, [dispatch, historyView])

  const setHistoryView = useCallback(async (view: OikosHistoryView) => {
    if (view === historyView) {
      return
    }
    setHistoryViewState(view)
    if (state.initialized && !messagesPreHydratedRef.current) {
      await loadOikosHistory({ view })
    }
  }, [historyView, loadOikosHistory, state.initialized])

  const checkForActiveRun = useCallback(async () => {
    try {
      logger.info('[useOikosApp] Checking for active run...')
      const response = await fetchWithRefresh(toAbsoluteUrl(`${CONFIG.OIKOS_API_BASE}/runs/active`), {
        credentials: 'include',
      })

      if (response.status === 204) {
        logger.info('[useOikosApp] No active run found')
        return null
      }

      if (!response.ok) {
        throw new Error(`Failed to check active run: ${response.status}`)
      }

      const data = await response.json()
      logger.info(`[useOikosApp] Found active run: ${data.run_id}`)
      return data.run_id
    } catch (error) {
      logger.warn('[useOikosApp] Failed to check for active run:', error)
      return null
    }
  }, [])

  const reconnectToRun = useCallback(async (runId: number) => {
    if (!oikosChatRef.current) {
      logger.warn('[useOikosApp] Cannot reconnect - oikos chat not initialized')
      return
    }

    try {
      logger.info(`[useOikosApp] Reconnecting to run ${runId}...`)
      updateState({ reconnecting: true })
      await oikosChatRef.current.attachToRun(runId)
      logger.info(`[useOikosApp] Reconnected to run ${runId}`)

      // Reload history to show the response that completed during reconnection
      // The response is stored in the database, so this will pick it up
      logger.info('[useOikosApp] Reloading history after reconnection...')
      await loadOikosHistory()
    } catch (error) {
      logger.error('[useOikosApp] Failed to reconnect to run:', error)
    } finally {
      updateState({ reconnecting: false })
    }
  }, [updateState, loadOikosHistory])

  // Main initialization
  const initialize = useCallback(async () => {
    if (state.initialized || initStartedRef.current) return
    initStartedRef.current = true

    logger.info('[useOikosApp] Initializing...')

    try {
      // 1. Initialize OikosClient
      await initializeOikosClient()

      // 2. Fetch bootstrap
      await fetchBootstrap()

      // 3. Initialize context
      await initializeContext()

      // 4. Initialize OikosChatController
      oikosChatRef.current = new OikosChatController(
        { maxRetries: 3 },
        {
          onStreamingTextChange: handleStreamingTextChange,
          onAssistantMessageUpdate: handleAssistantMessageUpdate,
          onToast: handleToast,
        },
      )
      await oikosChatRef.current.initialize()

      // 5. Check for active run and reconnect if found.
      // Do this before loading history so a refresh can reattach to a running SSE stream ASAP.
      const activeRunId = await checkForActiveRun()
      if (activeRunId) {
        logger.info(`[useOikosApp] Found active run ${activeRunId}, reconnecting...`)
        // Show UI immediately before SSE connects
        commisProgressStore.setReconnecting(activeRunId)
        await reconnectToRun(activeRunId)
      } else {
        // No active run → load history for the chat UI.
        await loadOikosHistory()
      }

      // 6. Set up voice listeners (turn-based mode skips realtime listeners)
      if (VOICE_INPUT_MODE === 'realtime') {
        setupVoiceListeners()
      } else {
        setVoiceStatus('ready')
        dispatch({ type: 'SET_VOICE_MODE', mode: 'push-to-talk' })
      }

      updateState({ initialized: true })
      logger.info('[useOikosApp] Initialization complete')
    } catch (error) {
      logger.error('[useOikosApp] Initialization failed:', error)
      initStartedRef.current = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- setupVoiceListeners defined below, circular dep intentional
  }, [
    state.initialized,
    initializeOikosClient,
    fetchBootstrap,
    initializeContext,
    loadOikosHistory,
    checkForActiveRun,
    reconnectToRun,
    updateState,
    handleStreamingTextChange,
    handleAssistantMessageUpdate,
    handleToast,
  ])

  // Set up voice controller event listeners
  const setupVoiceListeners = useCallback(() => {
    const handleVoiceEvent = (event: VoiceEvent) => {
      switch (event.type) {
        case 'stateChange': {
          const voiceState = event.state
          if (voiceState.active || voiceState.vadActive) {
            setVoiceStatus('listening')
          } else if (voiceController.isConnected()) {
            setVoiceStatus('ready')
          }

          // Update voice mode
          const mode = voiceState.handsFree ? 'hands-free' : 'push-to-talk'
          dispatch({ type: 'SET_VOICE_MODE', mode })
          break
        }

        case 'transcript':
          if (!event.isFinal) {
            dispatch({ type: 'SET_USER_TRANSCRIPT_PREVIEW', text: event.text })
          } else {
            dispatch({ type: 'SET_USER_TRANSCRIPT_PREVIEW', text: '' })
            // Send final transcript to oikos
            handleUserTranscript(event.text)
          }
          break

        case 'vadStateChange':
          if (event.active) {
            feedbackSystem.playVoiceTick()
          }
          break

        case 'error':
          setVoiceStatus('error')
          break
      }
    }

    voiceController.addListener(handleVoiceEvent)
    return () => voiceController.removeListener(handleVoiceEvent)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- handleUserTranscript defined below, circular dep intentional
  }, [dispatch, setVoiceStatus])

  // Handle user voice transcript
  const handleUserTranscript = useCallback(async (text: string) => {
    const finalText = text.trim()
    if (!finalText) return

    try {
      await sendText(finalText)
    } catch (error) {
      logger.error('[useOikosApp] Failed to send voice transcript:', error)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- sendText defined below, stable
  }, [])

  // ============= Connection =============

  const connect = useCallback(async () => {
    if (VOICE_INPUT_MODE !== 'realtime') {
      logger.info('[useOikosApp] Voice connect skipped (turn-based mode)')
      setVoiceStatus('ready')
      return
    }
    if (state.connecting || state.connected) return

    updateState({ connecting: true })
    setVoiceStatus('connecting')
    logger.info('[useOikosApp] Connecting...')

    try {
      // 1. Acquire microphone
      const micStream = await audioController.requestMicrophone()
      audioController.muteMicrophone() // Privacy-critical: mute immediately

      // 2. Validate context
      if (!state.currentContext) {
        throw new Error('No active context loaded')
      }

      // 3. Load history if needed
      if (!lastOikosTurnsRef.current.length) {
        await loadOikosHistory()
      }

      // 4. Bootstrap session
      // Realtime voice is a legacy lane. Keep the WebRTC SDK out of the normal
      // turn-based chat bundle so public web visits never load it by default.
      const { bootstrapSession } = await loadRealtimeBootstrap()
      const bootstrapResult = await bootstrapSession({
        context: state.currentContext,
        history: lastOikosTurnsRef.current,
        mediaStream: micStream,
        audioElement: undefined,
        tools: [], // v2.1: Realtime is I/O only, no tools
        onTokenRequest: getSessionToken,
        realtimeHistoryTurns: state.currentContext.settings?.realtimeHistoryTurns ?? 8,
      })

      const { session, hydratedItemCount } = bootstrapResult
      lastBootstrapResultRef.current = bootstrapResult

      logger.info(`[useOikosApp] Session bootstrapped, ${hydratedItemCount} items hydrated`)

      // 5. Set up session events
      setupSessionEvents(session)

      // 6. Wire up controllers
      voiceController.setSession(session)
      voiceController.setMicrophoneStream(micStream)

      // 7. Update state
      updateState({ connecting: false, connected: true })
      setVoiceStatus('ready')
      dispatch({ type: 'SET_CONNECTED', connected: true })

      // Set voice mode for PTT
      voiceController.transitionToVoice({ handsFree: false })

      // Audio feedback
      feedbackSystem.playConnectChime()

      logger.info('[useOikosApp] Connected successfully')
    } catch (error: unknown) {
      logger.error('[useOikosApp] Connection failed:', error)

      audioController.releaseMicrophone()
      updateState({ connecting: false, connected: false })
      setVoiceStatus('error')
      dispatch({ type: 'SET_CONNECTED', connected: false })

      feedbackSystem.playErrorTone()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- setupSessionEvents defined below, circular dep intentional
  }, [state.connecting, state.connected, state.currentContext, dispatch, updateState, setVoiceStatus, loadOikosHistory])

  const disconnect = useCallback(async () => {
    if (VOICE_INPUT_MODE !== 'realtime') {
      setVoiceStatus('ready')
      return
    }
    logger.info('[useOikosApp] Disconnecting...')

    audioController.setListeningMode(false)

    try {
      oikosChatRef.current?.cancel()
      const { sessionHandler } = await loadRealtimeSessionHandler()
      await sessionHandler.disconnect()

      voiceController.setSession(null)
      voiceController.reset()
      audioController.dispose()

      updateState({ connected: false })
      setVoiceStatus('idle')
      dispatch({ type: 'SET_CONNECTED', connected: false })
      dispatch({ type: 'SET_USER_TRANSCRIPT_PREVIEW', text: '' })

      logger.info('[useOikosApp] Disconnected')
    } catch (error) {
      logger.error('[useOikosApp] Disconnect error:', error)
    }
  }, [dispatch, updateState, setVoiceStatus])

  const reconnect = useCallback(async () => {
    if (VOICE_INPUT_MODE !== 'realtime') {
      setVoiceStatus('ready')
      return
    }
    await disconnect()
    await new Promise(resolve => setTimeout(resolve, 100))
    await connect()
  }, [connect, disconnect])

  // Set up OpenAI session events
  const setupSessionEvents = useCallback((session: { on: (event: string, callback: (event: any) => void) => void }) => {
    session.on('transport_event', async (event: any) => {
      const t = event.type || ''

      // Forward to voice controller
      if (t === 'conversation.item.input_audio_transcription.delta') {
        voiceController.handleTranscript(event.delta || '', false)
      }
      if (t === 'conversation.item.input_audio_transcription.completed') {
        voiceController.handleTranscript(event.transcript || '', true)
      }
      if (t === 'input_audio_buffer.speech_started') {
        voiceController.handleSpeechStart()
      }
      if (t === 'input_audio_buffer.speech_stopped') {
        voiceController.handleSpeechStop()
      }

      // v2.1: Ignore Realtime responses - Oikos is the only brain
      if (t.startsWith('response.')) {
        logger.warn(`[useOikosApp] Ignoring Realtime response event: ${t}`)
        return
      }

      // User voice committed - add placeholder for correct ordering
      if (t === 'conversation.item.done') {
        const item = event.item
        if (item?.role === 'user' && item?.id) {
          const contentType = item.content?.[0]?.type
          if (contentType === 'input_audio') {
            dispatch({
              type: 'ADD_MESSAGE',
              message: {
                id: uuid(),
                role: 'user',
                content: '...',
                timestamp: new Date(),
                itemId: item.id,
              },
            })
          }
        }
      }

      // User voice transcript ready - update placeholder
      if (t === 'conversation.item.input_audio_transcription.completed') {
        const itemId = event.item_id
        const transcript = event.transcript || ''
        if (itemId && transcript) {
          dispatch({ type: 'UPDATE_MESSAGE', itemId, content: transcript })
        }
      }

      // Error handling
      if (t === 'error') {
        const errorMsg = event.error?.message || event.error?.code || 'Unknown error'
        logger.error('[useOikosApp] Session error:', errorMsg)
      }
    })
  }, [dispatch])

  // Get session token for OpenAI
  const getSessionToken = async (): Promise<string> => {
    const r = await fetchWithRefresh(toAbsoluteUrl(`${CONFIG.OIKOS_API_BASE}/session`), {
      credentials: 'include',
    })
    if (!r.ok) throw new Error('Failed to get session token')
    const js = await r.json()
    return js.value || js.client_secret?.value
  }

  // ============= Text Messaging =============

  const sendText = useCallback(async (
    text: string,
    messageId?: string,
    options?: { model?: string; reasoning_effort?: string }
  ) => {
    if (!oikosChatRef.current) {
      throw new Error('Oikos chat not initialized')
    }

    const msgId = messageId || uuid()

    // Set message ID for timeline tracking
    timelineLogger.setMessageId(msgId)

    // Emit text_channel:sent event for timeline tracking
    eventBus.emit('text_channel:sent', {
      text: text,
      timestamp: Date.now(),
    })

    const model = options?.model || appState.preferences.chat_model || DEFAULT_CHAT_PREFERENCES.chat_model
    const reasoning_effort =
      options?.reasoning_effort
      || appState.preferences.reasoning_effort
      || DEFAULT_CHAT_PREFERENCES.reasoning_effort

    logger.info(`[useOikosApp] Sending text, model: ${model}, messageId: ${msgId}`)
    await oikosChatRef.current.sendMessage(text, msgId, { model, reasoning_effort })
  }, [appState.preferences.chat_model, appState.preferences.reasoning_effort])

  const clearHistory = useCallback(async () => {
    if (!oikosChatRef.current) return

    try {
      await oikosChatRef.current.clearHistory()
      dispatch({ type: 'SET_MESSAGES', messages: [] })
      dispatch({ type: 'SET_STREAMING_CONTENT', content: '' })
      logger.info('[useOikosApp] History cleared')
    } catch (error) {
      logger.error('[useOikosApp] Failed to clear history:', error)
      throw error
    }
  }, [dispatch])

  // ============= Voice Controls =============

  const handlePTTPress = useCallback(() => {
    if (VOICE_INPUT_MODE !== 'realtime') return
    if (!voiceController.isConnected()) {
      logger.warn('[useOikosApp] PTT press ignored - not connected')
      return
    }
    voiceController.startPTT()
  }, [])

  const handlePTTRelease = useCallback(() => {
    if (VOICE_INPUT_MODE !== 'realtime') return
    if (!voiceController.isConnected()) return
    voiceController.stopPTT()
  }, [])

  const toggleHandsFree = useCallback(() => {
    voiceController.setHandsFree(!voiceController.getState().handsFree)
  }, [])

  // ============= Effects =============

  // Initialize on mount
  useEffect(() => {
    initialize()
  }, [initialize])

  // ============= Return API =============

  return {
    // State
    initialized: state.initialized,
    connecting: state.connecting,
    connected: state.connected,
    reconnecting: state.reconnecting,
    voiceStatus: state.voiceStatus,
    bootstrap: state.bootstrap,
    historyView,

    // Connection
    connect,
    disconnect,
    reconnect,
    isConnected: () => voiceController.isConnected(),

    // Voice
    handlePTTPress,
    handlePTTRelease,
    toggleHandsFree,

    // Text
    sendText,
    clearHistory,
    setHistoryView,
    reloadHistory: loadOikosHistory,

    // Controllers (for advanced usage)
    voiceController,
    audioController,
  }
}
