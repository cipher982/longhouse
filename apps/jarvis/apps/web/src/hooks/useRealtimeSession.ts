/**
 * useRealtimeSession hook - Bridge to existing voice/session controllers
 *
 * This hook bridges the existing vanilla TypeScript controllers with React.
 * It imports the singleton controllers and syncs their state to React.
 */

import { useEffect, useCallback, useRef, useState } from 'react'
import { useAppDispatch } from '../context'

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

  // Initialize controllers on mount
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
      options.onError?.(error)
    })

    // Subscribe to voice controller events
    const handleVoiceEvent = (event: VoiceEvent) => {
      switch (event.type) {
        case 'stateChange':
          // Map voice state to React state
          const voiceState = event.state
          if (voiceState.active) {
            dispatch({ type: 'SET_VOICE_STATUS', status: 'listening' })
          } else if (voiceState.vadActive) {
            dispatch({ type: 'SET_VOICE_STATUS', status: 'listening' })
          } else {
            dispatch({ type: 'SET_VOICE_STATUS', status: 'idle' })
          }

          // Update voice mode
          const mode = voiceState.handsFree ? 'hands-free' : 'push-to-talk'
          dispatch({ type: 'SET_VOICE_MODE', mode })
          break

        case 'transcript':
          options.onTranscript?.(event.text, event.isFinal)
          break

        case 'error':
          dispatch({ type: 'SET_VOICE_STATUS', status: 'error' })
          options.onError?.(event.error)
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
            options.onConnected?.()
          } else {
            options.onDisconnected?.()
          }
          break

        case 'STREAMING_TEXT_CHANGED':
          dispatch({ type: 'SET_STREAMING_CONTENT', content: event.text })
          break
      }
    }

    stateManager.addListener(handleStateChange)

    // Cleanup
    return () => {
      voiceController.removeListener(handleVoiceEvent)
      stateManager.removeListener(handleStateChange)
    }
  }, [dispatch, options])

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
        dispatch({ type: 'SET_CONNECTED', connected: true })
        options.onConnected?.()
      } catch (error) {
        console.error('[useRealtimeSession] Auto-connect failed:', error)
        dispatch({ type: 'SET_VOICE_STATUS', status: 'error' })
        setConnectionError(error as Error)
        options.onError?.(error as Error)
        // Allow retry by resetting the flag after failure
        connectAttemptedRef.current = false
      }
    }, 100)

    return () => clearTimeout(timeoutId)
  }, [autoConnect, dispatch, options])

  // Connect to realtime session
  const connect = useCallback(async () => {
    try {
      dispatch({ type: 'SET_VOICE_STATUS', status: 'connecting' })
      setConnectionError(null)
      await appController.connect()
      dispatch({ type: 'SET_CONNECTED', connected: true })
      options.onConnected?.()
    } catch (error) {
      console.error('[useRealtimeSession] Connect failed:', error)
      dispatch({ type: 'SET_VOICE_STATUS', status: 'error' })
      setConnectionError(error as Error)
      options.onError?.(error as Error)
    }
  }, [dispatch, options])

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
      dispatch({ type: 'SET_CONNECTED', connected: false })
      dispatch({ type: 'SET_VOICE_STATUS', status: 'idle' })
      options.onDisconnected?.()
    } catch (error) {
      console.error('[useRealtimeSession] Disconnect failed:', error)
      options.onError?.(error as Error)
    }
  }, [dispatch, options])

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
