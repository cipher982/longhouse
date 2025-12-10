/**
 * useRealtimeSession hook - Bridge to existing voice/session controllers
 *
 * This hook bridges the existing vanilla TypeScript controllers with React.
 * It imports the singleton controllers and syncs their state to React.
 *
 * The controllers emit events via stateManager, and this hook subscribes
 * to those events and dispatches to React context.
 */

import { useEffect, useCallback, useRef, useState } from 'react'
import { useAppDispatch, type ChatMessage } from '../context'

// Import existing controllers (singleton instances)
import { voiceController, type VoiceEvent } from '../../lib/voice-controller'
import { audioController } from '../../lib/audio-controller'
import { appController } from '../../lib/app-controller'
import { sessionHandler } from '../../lib/session-handler'
import { conversationController } from '../../lib/conversation-controller'
import { stateManager, type StateChangeEvent } from '../../lib/state-manager'

export interface UseRealtimeSessionOptions {
  /** Automatically connect on mount (default: true) */
  autoConnect?: boolean
  onConnected?: () => void
  onDisconnected?: () => void
  onTranscript?: (text: string, isFinal: boolean) => void
  onAssistantMessage?: (text: string) => void
  onError?: (error: Error) => void
}

/**
 * Hook that bridges existing controllers with React state
 */
export function useRealtimeSession(options: UseRealtimeSessionOptions = {}) {
  const dispatch = useAppDispatch()
  const initializedRef = useRef(false)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const optionsRef = useRef(options)
  optionsRef.current = options  // Always keep ref up to date

  // One-time controller initialization
  useEffect(() => {
    if (initializedRef.current) return
    initializedRef.current = true

    console.log('[useRealtimeSession] Initializing controllers')

    // Get audio element
    audioRef.current = document.getElementById('remoteAudio') as HTMLAudioElement

    // Initialize audio controller if we have the elements
    const pttBtn = document.getElementById('pttBtn') as HTMLButtonElement
    if (pttBtn && audioRef.current) {
      audioController.initialize(audioRef.current, pttBtn)
    }

    // Initialize app controller (this sets up JarvisClient, etc.)
    appController.initialize().catch((error) => {
      console.error('[useRealtimeSession] App controller init failed:', error)
      optionsRef.current.onError?.(error)
    })
  }, [dispatch])  // dispatch needed for history callback

  // Subscribe to controller events - SEPARATE effect so it always runs
  useEffect(() => {
    // Subscribe to voice controller events
    const handleVoiceEvent = (event: VoiceEvent) => {
      switch (event.type) {
        case 'stateChange':
          const voiceState = event.state
          // Update voice status based on active state
          if (voiceState.active || voiceState.vadActive) {
            dispatch({ type: 'SET_VOICE_STATUS', status: 'listening' })
          } else if (voiceController.isConnected()) {
            // When PTT released but still connected, go back to ready
            dispatch({ type: 'SET_VOICE_STATUS', status: 'ready' })
          }

          // Update voice mode
          const mode = voiceState.handsFree ? 'hands-free' : 'push-to-talk'
          dispatch({ type: 'SET_VOICE_MODE', mode })
          break

        case 'transcript':
          // Pass transcript to callback AND update streaming content for live preview
          optionsRef.current.onTranscript?.(event.text, event.isFinal)
          if (!event.isFinal) {
            // Show interim transcript as user typing preview
            dispatch({ type: 'SET_USER_TRANSCRIPT_PREVIEW', text: event.text })
          } else {
            // Clear preview when final
            dispatch({ type: 'SET_USER_TRANSCRIPT_PREVIEW', text: '' })
          }
          break

        case 'error':
          dispatch({ type: 'SET_VOICE_STATUS', status: 'error' })
          optionsRef.current.onError?.(event.error)
          break
      }
    }

    voiceController.addListener(handleVoiceEvent)

    // Subscribe to state manager events
    const handleStateChange = (event: StateChangeEvent) => {
      switch (event.type) {
        case 'SESSION_CHANGED':
          dispatch({ type: 'SET_CONNECTED', connected: event.session !== null })
          if (event.session) {
            optionsRef.current.onConnected?.()
          } else {
            // Clear stale voice preview on disconnect
            dispatch({ type: 'SET_USER_TRANSCRIPT_PREVIEW', text: '' })
            optionsRef.current.onDisconnected?.()
          }
          break

        case 'STREAMING_TEXT_CHANGED':
          dispatch({ type: 'SET_STREAMING_CONTENT', content: event.text })
          break

        case 'VOICE_STATUS_CHANGED':
          dispatch({ type: 'SET_VOICE_STATUS', status: event.status })
          break

        case 'MESSAGE_FINALIZED':
          // Add the finalized assistant message to React state
          dispatch({ type: 'ADD_MESSAGE', message: event.message as ChatMessage })
          break

        case 'USER_VOICE_COMMITTED':
          // Add placeholder user message immediately for correct ordering
          dispatch({
            type: 'ADD_MESSAGE',
            message: {
              id: crypto.randomUUID(),
              role: 'user',
              content: '...',
              timestamp: new Date(),
              itemId: event.itemId,
            },
          })
          break

        case 'USER_VOICE_TRANSCRIPT':
          // Update placeholder with actual transcript
          dispatch({ type: 'UPDATE_MESSAGE', itemId: event.itemId, content: event.transcript })
          break

        case 'HISTORY_LOADED': {
          // Convert turns to ChatMessages for React state
          const messages: ChatMessage[] = []
          for (const turn of event.history) {
            if (turn.userTranscript) {
              messages.push({
                id: turn.id || crypto.randomUUID(),
                role: 'user',
                content: turn.userTranscript,
                timestamp: turn.timestamp ? new Date(turn.timestamp) : new Date(),
              })
            }
            if (turn.assistantResponse) {
              messages.push({
                id: `${turn.id}-asst` || crypto.randomUUID(),
                role: 'assistant',
                content: turn.assistantResponse,
                timestamp: turn.timestamp ? new Date(turn.timestamp) : new Date(),
              })
            }
          }
          dispatch({ type: 'SET_MESSAGES', messages })
          break
        }

        case 'TOAST':
          // Could dispatch to a toast system in React context
          // For now, just log it
          console.log(`[Toast] ${event.variant}: ${event.message}`)
          break

        case 'CONNECTION_ERROR':
          // Clear stale voice preview on connection error
          dispatch({ type: 'SET_USER_TRANSCRIPT_PREVIEW', text: '' })
          dispatch({ type: 'SET_VOICE_STATUS', status: 'error' })
          optionsRef.current.onError?.(event.error)
          break
      }
    }

    stateManager.addListener(handleStateChange)

    // Cleanup
    return () => {
      voiceController.removeListener(handleVoiceEvent)
      stateManager.removeListener(handleStateChange)
    }
  }, [dispatch])  // Only dispatch - options accessed via ref

  // Auto-connect on mount if enabled (default: true)
  const autoConnect = options.autoConnect !== false
  const connectAttemptedRef = useRef(false)
  const [connectionError, setConnectionError] = useState<Error | null>(null)

  useEffect(() => {
    if (!autoConnect || connectAttemptedRef.current || !initializedRef.current) {
      return
    }

    connectAttemptedRef.current = true
    console.log('[useRealtimeSession] Auto-connecting to realtime session')

    // Slight delay to ensure DOM elements (pttBtn, remoteAudio) are mounted
    const timeoutId = setTimeout(async () => {
      try {
        dispatch({ type: 'SET_VOICE_STATUS', status: 'connecting' })
        setConnectionError(null)
        await appController.connect()
        // NOTE: Don't call onConnected here - SESSION_CHANGED listener handles it
        // This prevents duplicate callbacks when both this effect AND the
        // state change listener would otherwise both call onConnected
        dispatch({ type: 'SET_VOICE_STATUS', status: 'ready' })
      } catch (error) {
        console.error('[useRealtimeSession] Auto-connect failed:', error)
        dispatch({ type: 'SET_VOICE_STATUS', status: 'error' })
        setConnectionError(error as Error)
        optionsRef.current.onError?.(error as Error)
        // Allow retry by resetting the flag after failure
        connectAttemptedRef.current = false
      }
    }, 100)

    return () => clearTimeout(timeoutId)
  }, [autoConnect, dispatch])

  // Connect to realtime session
  const connect = useCallback(async () => {
    try {
      dispatch({ type: 'SET_VOICE_STATUS', status: 'connecting' })
      setConnectionError(null)
      await appController.connect()
      // NOTE: Don't call onConnected here - SESSION_CHANGED listener handles it
      // This prevents duplicate callbacks
      dispatch({ type: 'SET_VOICE_STATUS', status: 'ready' })
    } catch (error) {
      console.error('[useRealtimeSession] Connect failed:', error)
      dispatch({ type: 'SET_VOICE_STATUS', status: 'error' })
      setConnectionError(error as Error)
      optionsRef.current.onError?.(error as Error)
    }
  }, [dispatch])

  // Reconnect after failure - resets state and attempts connection
  const reconnect = useCallback(async () => {
    console.log('[useRealtimeSession] Reconnecting...')
    connectAttemptedRef.current = false
    setConnectionError(null)
    dispatch({ type: 'SET_VOICE_STATUS', status: 'idle' })
    // Slight delay then connect
    await new Promise((resolve) => setTimeout(resolve, 100))
    return connect()
  }, [connect, dispatch])

  // Disconnect from realtime session
  const disconnect = useCallback(() => {
    try {
      appController.disconnect()
      // NOTE: SESSION_CHANGED listener handles SET_CONNECTED and onDisconnected
      dispatch({ type: 'SET_VOICE_STATUS', status: 'idle' })
    } catch (error) {
      console.error('[useRealtimeSession] Disconnect failed:', error)
      optionsRef.current.onError?.(error as Error)
    }
  }, [dispatch])

  // Check if connected
  const isConnected = useCallback(() => {
    return voiceController.isConnected()
  }, [])

  // PTT press handler - only works when connected
  const handlePTTPress = useCallback(() => {
    if (!voiceController.isConnected()) {
      console.warn('[useRealtimeSession] PTT press ignored - not connected')
      return
    }
    voiceController.startPTT()
  }, [])

  // PTT release handler - only works when connected
  const handlePTTRelease = useCallback(() => {
    if (!voiceController.isConnected()) {
      return
    }
    voiceController.stopPTT()
  }, [])

  // Toggle hands-free mode
  const toggleHandsFree = useCallback(() => {
    voiceController.setHandsFree(!voiceController.getState().handsFree)
  }, [])

  return {
    // State
    isConnected,
    connectionError,

    // Actions
    connect,
    disconnect,
    reconnect,
    handlePTTPress,
    handlePTTRelease,
    toggleHandsFree,

    // Controllers (for advanced usage)
    voiceController,
    audioController,
    appController,
    sessionHandler,
    conversationController,
  }
}
