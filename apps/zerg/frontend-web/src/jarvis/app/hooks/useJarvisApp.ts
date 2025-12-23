/**
 * useJarvisApp - Unified Jarvis Application Hook
 *
 * This hook manages the entire Jarvis application lifecycle:
 * - Initialization (JarvisClient, bootstrap, context, history)
 * - Voice connection (mic, session, voice controller)
 * - Text messaging (via SupervisorChatController)
 * - State synchronization (directly to React context, no stateManager bridge)
 *
 * Replaces the old architecture:
 *   Controllers → stateManager → useRealtimeSession → dispatch → context
 *
 * New architecture:
 *   Controllers → useJarvisApp → dispatch → context
 */

import { useEffect, useCallback, useRef, useState } from 'react'
import { useAppDispatch, type ChatMessage } from '../context'
import { logger, getJarvisClient, type JarvisAPIClient } from '../../core'
import type { ConversationTurn } from '../../data'

// Import controllers (keep these - they're pure business logic)
import { voiceController, type VoiceEvent } from '../../lib/voice-controller'
import { audioController } from '../../lib/audio-controller'
import { sessionHandler } from '../../lib/session-handler'
import { feedbackSystem } from '../../lib/feedback-system'
import { SupervisorChatController } from '../../lib/supervisor-chat-controller'
import { bootstrapSession, type BootstrapResult } from '../../lib/session-bootstrap'
import { contextLoader } from '../../contexts/context-loader'
import type { VoiceAgentConfig } from '../../contexts/types'
import { getZergApiUrl, CONFIG, toAbsoluteUrl } from '../../lib/config'
import { uuid } from '../../lib/uuid'
// Keep stateManager for streaming events from supervisor-chat-controller
// TODO: Refactor supervisor-chat-controller to use callbacks instead
import { stateManager, type StateChangeEvent } from '../../lib/state-manager'

// Types (previously in state-manager.ts)
export interface ModelInfo {
  id: string
  display_name: string
  description: string
}

export interface ChatPreferences {
  chat_model: string
  reasoning_effort: 'none' | 'low' | 'medium' | 'high'
}

export interface BootstrapData {
  prompt: string
  enabled_tools: Array<{ name: string; description: string }>
  user_context: {
    display_name?: string
    role?: string
    location?: string
    servers?: Array<{ name: string; purpose: string }>
  }
  available_models: ModelInfo[]
  preferences: ChatPreferences
}

export interface UseJarvisAppOptions {
  autoConnect?: boolean
  onConnected?: () => void
  onDisconnected?: () => void
  onTranscript?: (text: string, isFinal: boolean) => void
  onError?: (error: Error) => void
}

interface JarvisAppState {
  initialized: boolean
  connecting: boolean
  connected: boolean
  voiceStatus: 'idle' | 'connecting' | 'ready' | 'listening' | 'processing' | 'speaking' | 'error'
  bootstrap: BootstrapData | null
  currentContext: VoiceAgentConfig | null
  jarvisClient: JarvisAPIClient | null
}

/**
 * Main hook for Jarvis application
 */
export function useJarvisApp(options: UseJarvisAppOptions = {}) {
  const dispatch = useAppDispatch()
  const optionsRef = useRef(options)
  optionsRef.current = options

  // Local state (not in React context - these are internal implementation details)
  const [state, setState] = useState<JarvisAppState>({
    initialized: false,
    connecting: false,
    connected: false,
    voiceStatus: 'idle',
    bootstrap: null,
    currentContext: null,
    jarvisClient: null,
  })

  // Refs for singleton instances
  const supervisorChatRef = useRef<SupervisorChatController | null>(null)
  const lastBootstrapResultRef = useRef<BootstrapResult | null>(null)
  const lastSupervisorTurnsRef = useRef<ConversationTurn[]>([])
  const initStartedRef = useRef(false)

  // Helper to update internal state
  const updateState = useCallback((updates: Partial<JarvisAppState>) => {
    setState(prev => ({ ...prev, ...updates }))
  }, [])

  // Update voice status in both local state and React context
  const setVoiceStatus = useCallback((status: JarvisAppState['voiceStatus']) => {
    updateState({ voiceStatus: status })
    dispatch({ type: 'SET_VOICE_STATUS', status })
  }, [dispatch, updateState])

  // ============= Initialization =============

  const initializeJarvisClient = useCallback(async () => {
    try {
      const zergApiUrl = getZergApiUrl()
      logger.info(`[useJarvisApp] Initializing JarvisClient with URL: ${zergApiUrl}`)

      const jarvisClient = getJarvisClient(zergApiUrl)
      updateState({ jarvisClient })

      const isAuthed = await jarvisClient.isAuthenticated()
      if (isAuthed) {
        logger.info('[useJarvisApp] JarvisClient authenticated')
      } else {
        logger.warn('[useJarvisApp] Not authenticated - log in to enable supervisor features')
      }

      return jarvisClient
    } catch (error) {
      logger.error('[useJarvisApp] Failed to initialize JarvisClient:', error)
      return null
    }
  }, [updateState])

  const fetchBootstrap = useCallback(async () => {
    try {
      logger.info('[useJarvisApp] Fetching bootstrap configuration...')
      const response = await fetch(toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/bootstrap`), {
        credentials: 'include',
      })

      if (!response.ok) {
        throw new Error(`Bootstrap fetch failed: ${response.status}`)
      }

      const bootstrap = await response.json() as BootstrapData
      updateState({ bootstrap })

      // Update React context with bootstrap data
      if (bootstrap.available_models) {
        dispatch({ type: 'SET_AVAILABLE_MODELS', models: bootstrap.available_models })
      }
      if (bootstrap.preferences) {
        dispatch({ type: 'SET_PREFERENCES', preferences: bootstrap.preferences })
      }

      logger.info('[useJarvisApp] Bootstrap configuration loaded')
      return bootstrap
    } catch (error) {
      logger.error('[useJarvisApp] Failed to fetch bootstrap:', error)
      return null
    }
  }, [dispatch, updateState])

  const initializeContext = useCallback(async () => {
    try {
      const contextName = await contextLoader.autoDetectContext()
      logger.info(`[useJarvisApp] Loading context: ${contextName}`)

      const currentContext = await contextLoader.loadContext(contextName)
      updateState({ currentContext })

      // Set initial conversation state
      dispatch({ type: 'SET_CONVERSATION_ID', id: null })
      dispatch({
        type: 'SET_CONVERSATIONS',
        conversations: [{ id: 'server', name: 'Current', meta: 'Server', active: true }],
      })

      logger.info(`[useJarvisApp] Context initialized: ${contextName}`)
      return currentContext
    } catch (error) {
      logger.error('[useJarvisApp] Failed to initialize context:', error)
      throw error
    }
  }, [dispatch, updateState])

  const loadSupervisorHistory = useCallback(async () => {
    if (!supervisorChatRef.current) return []

    try {
      logger.info('[useJarvisApp] Loading Supervisor chat history...')
      const messages = await supervisorChatRef.current.loadHistory(50)

      if (messages.length > 0) {
        const history: ConversationTurn[] = messages.map(msg => ({
          id: uuid(),
          timestamp: msg.timestamp,
          userTranscript: msg.role === 'user' ? msg.content : undefined,
          assistantResponse: msg.role === 'assistant' ? msg.content : undefined,
        }))

        lastSupervisorTurnsRef.current = history

        // Convert to ChatMessages for React context
        const chatMessages: ChatMessage[] = []
        for (const turn of history) {
          if (turn.userTranscript) {
            chatMessages.push({
              id: turn.id || uuid(),
              role: 'user',
              content: turn.userTranscript,
              timestamp: turn.timestamp ? new Date(turn.timestamp) : new Date(),
            })
          }
          if (turn.assistantResponse) {
            chatMessages.push({
              id: `${turn.id}-asst`,
              role: 'assistant',
              content: turn.assistantResponse,
              timestamp: turn.timestamp ? new Date(turn.timestamp) : new Date(),
            })
          }
        }

        dispatch({ type: 'SET_MESSAGES', messages: chatMessages })
        logger.info(`[useJarvisApp] Loaded ${messages.length} messages from history`)
        return history
      }

      lastSupervisorTurnsRef.current = []
      return []
    } catch (error) {
      logger.warn('[useJarvisApp] Failed to load history (non-fatal):', error)
      return []
    }
  }, [dispatch])

  // Main initialization
  const initialize = useCallback(async () => {
    if (state.initialized || initStartedRef.current) return
    initStartedRef.current = true

    logger.info('[useJarvisApp] Initializing...')

    try {
      // 1. Initialize JarvisClient
      await initializeJarvisClient()

      // 2. Fetch bootstrap
      await fetchBootstrap()

      // 3. Initialize context
      await initializeContext()

      // 4. Initialize SupervisorChatController
      supervisorChatRef.current = new SupervisorChatController({ maxRetries: 3 })
      await supervisorChatRef.current.initialize()

      // 5. Load history
      await loadSupervisorHistory()

      // 6. Set up event listeners
      setupVoiceListeners()
      setupStreamingListeners()

      updateState({ initialized: true })
      logger.info('[useJarvisApp] Initialization complete')
    } catch (error) {
      logger.error('[useJarvisApp] Initialization failed:', error)
      initStartedRef.current = false
      optionsRef.current.onError?.(error as Error)
    }
  }, [state.initialized, initializeJarvisClient, fetchBootstrap, initializeContext, loadSupervisorHistory, updateState])

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
          optionsRef.current.onTranscript?.(event.text, event.isFinal)
          if (!event.isFinal) {
            dispatch({ type: 'SET_USER_TRANSCRIPT_PREVIEW', text: event.text })
          } else {
            dispatch({ type: 'SET_USER_TRANSCRIPT_PREVIEW', text: '' })
            // Send final transcript to supervisor
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
          optionsRef.current.onError?.(event.error)
          break
      }
    }

    voiceController.addListener(handleVoiceEvent)
    return () => voiceController.removeListener(handleVoiceEvent)
  }, [dispatch, setVoiceStatus])

  // Set up stateManager listeners for streaming events from supervisor-chat-controller
  const setupStreamingListeners = useCallback(() => {
    const handleStateChange = (event: StateChangeEvent) => {
      switch (event.type) {
        case 'STREAMING_TEXT_CHANGED':
          dispatch({ type: 'SET_STREAMING_CONTENT', content: event.text })
          break

        case 'MESSAGE_FINALIZED':
          if (event.message.correlationId) {
            dispatch({
              type: 'UPDATE_MESSAGE_BY_CORRELATION_ID',
              correlationId: event.message.correlationId,
              updates: {
                content: event.message.content,
                status: 'final',
                timestamp: event.message.timestamp,
              },
            })
          } else {
            dispatch({ type: 'ADD_MESSAGE', message: event.message as ChatMessage })
          }
          break

        case 'ASSISTANT_STATUS_CHANGED':
          dispatch({
            type: 'UPDATE_MESSAGE_BY_CORRELATION_ID',
            correlationId: event.correlationId,
            updates: {
              status: event.status as any,
              ...(event.content !== undefined ? { content: event.content } : {}),
              ...(event.usage !== undefined ? { usage: event.usage } : {}),
            },
          })
          break

        case 'TOAST':
          // Could dispatch to a toast system in React context
          logger.info(`[Toast] ${event.variant}: ${event.message}`)
          break
      }
    }

    stateManager.addListener(handleStateChange)
    return () => stateManager.removeListener(handleStateChange)
  }, [dispatch])

  // Handle user voice transcript
  const handleUserTranscript = useCallback(async (text: string) => {
    const finalText = text.trim()
    if (!finalText) return

    try {
      await sendText(finalText)
    } catch (error) {
      logger.error('[useJarvisApp] Failed to send voice transcript:', error)
    }
  }, [])

  // ============= Connection =============

  const connect = useCallback(async () => {
    if (state.connecting || state.connected) return

    updateState({ connecting: true })
    setVoiceStatus('connecting')
    logger.info('[useJarvisApp] Connecting...')

    try {
      // 1. Acquire microphone
      const micStream = await audioController.requestMicrophone()
      audioController.muteMicrophone() // Privacy-critical: mute immediately

      // 2. Validate context
      if (!state.currentContext) {
        throw new Error('No active context loaded')
      }

      // 3. Load history if needed
      if (!lastSupervisorTurnsRef.current.length) {
        await loadSupervisorHistory()
      }

      // 4. Bootstrap session
      const bootstrapResult = await bootstrapSession({
        context: state.currentContext,
        conversationId: null,
        history: lastSupervisorTurnsRef.current,
        mediaStream: micStream,
        audioElement: undefined,
        tools: [], // v2.1: Realtime is I/O only, no tools
        onTokenRequest: getSessionToken,
        realtimeHistoryTurns: state.currentContext.settings?.realtimeHistoryTurns ?? 8,
      })

      const { session, conversationId, hydratedItemCount } = bootstrapResult
      lastBootstrapResultRef.current = bootstrapResult

      logger.info(`[useJarvisApp] Session bootstrapped, ${hydratedItemCount} items hydrated`)

      // 5. Set up session events
      setupSessionEvents(session)

      // 6. Wire up controllers
      voiceController.setSession(session)
      voiceController.setMicrophoneStream(micStream)

      // 7. Update state
      updateState({ connecting: false, connected: true })
      setVoiceStatus('ready')
      dispatch({ type: 'SET_CONNECTED', connected: true })

      if (conversationId) {
        dispatch({ type: 'SET_CONVERSATION_ID', id: conversationId })
      }

      // Set voice mode for PTT
      voiceController.transitionToVoice({ handsFree: false })

      // Audio feedback
      feedbackSystem.playConnectChime()
      optionsRef.current.onConnected?.()

      logger.info('[useJarvisApp] Connected successfully')
    } catch (error: unknown) {
      logger.error('[useJarvisApp] Connection failed:', error)

      audioController.releaseMicrophone()
      updateState({ connecting: false, connected: false })
      setVoiceStatus('error')
      dispatch({ type: 'SET_CONNECTED', connected: false })

      feedbackSystem.playErrorTone()
      optionsRef.current.onError?.(error as Error)
    }
  }, [state.connecting, state.connected, state.currentContext, dispatch, updateState, setVoiceStatus, loadSupervisorHistory])

  const disconnect = useCallback(async () => {
    logger.info('[useJarvisApp] Disconnecting...')

    audioController.setListeningMode(false)

    try {
      supervisorChatRef.current?.cancel()
      await sessionHandler.disconnect()

      voiceController.setSession(null)
      voiceController.reset()
      audioController.dispose()

      updateState({ connected: false })
      setVoiceStatus('idle')
      dispatch({ type: 'SET_CONNECTED', connected: false })
      dispatch({ type: 'SET_USER_TRANSCRIPT_PREVIEW', text: '' })

      optionsRef.current.onDisconnected?.()
      logger.info('[useJarvisApp] Disconnected')
    } catch (error) {
      logger.error('[useJarvisApp] Disconnect error:', error)
    }
  }, [dispatch, updateState, setVoiceStatus])

  const reconnect = useCallback(async () => {
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

      // v2.1: Ignore Realtime responses - Supervisor is the only brain
      if (t.startsWith('response.')) {
        logger.warn(`[useJarvisApp] Ignoring Realtime response event: ${t}`)
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
        logger.error('[useJarvisApp] Session error:', errorMsg)
      }
    })
  }, [dispatch])

  // Get session token for OpenAI
  const getSessionToken = async (): Promise<string> => {
    const r = await fetch(toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/session`), {
      credentials: 'include',
    })
    if (!r.ok) throw new Error('Failed to get session token')
    const js = await r.json()
    return js.value || js.client_secret?.value
  }

  // ============= Text Messaging =============

  const sendText = useCallback(async (
    text: string,
    correlationId?: string,
    options?: { model?: string; reasoning_effort?: string }
  ) => {
    if (!supervisorChatRef.current) {
      throw new Error('Supervisor chat not initialized')
    }

    const cid = correlationId || uuid()

    // Get preferences from bootstrap
    const prefs = state.bootstrap?.preferences || { chat_model: 'gpt-5.1', reasoning_effort: 'none' }
    const model = options?.model || prefs.chat_model
    const reasoning_effort = options?.reasoning_effort || prefs.reasoning_effort

    logger.info(`[useJarvisApp] Sending text, model: ${model}, correlationId: ${cid}`)
    await supervisorChatRef.current.sendMessage(text, cid, { model, reasoning_effort })
  }, [state.bootstrap])

  const clearHistory = useCallback(async () => {
    if (!supervisorChatRef.current) return

    try {
      await supervisorChatRef.current.clearHistory()
      dispatch({ type: 'SET_MESSAGES', messages: [] })
      logger.info('[useJarvisApp] History cleared')
    } catch (error) {
      logger.error('[useJarvisApp] Failed to clear history:', error)
      throw error
    }
  }, [dispatch])

  // ============= Voice Controls =============

  const handlePTTPress = useCallback(() => {
    if (!voiceController.isConnected()) {
      logger.warn('[useJarvisApp] PTT press ignored - not connected')
      return
    }
    voiceController.startPTT()
  }, [])

  const handlePTTRelease = useCallback(() => {
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

  // Auto-connect if enabled
  useEffect(() => {
    if (options.autoConnect && state.initialized && !state.connected && !state.connecting) {
      connect()
    }
  }, [options.autoConnect, state.initialized, state.connected, state.connecting, connect])

  // ============= Return API =============

  return {
    // State
    initialized: state.initialized,
    connecting: state.connecting,
    connected: state.connected,
    voiceStatus: state.voiceStatus,
    bootstrap: state.bootstrap,

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

    // Controllers (for advanced usage)
    voiceController,
    audioController,
  }
}
