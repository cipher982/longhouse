/**
 * useRealtimeSession hook - Bridge to existing voice/session controllers
 *
 * This hook bridges the existing vanilla TypeScript controllers with React.
 * It imports the singleton controllers and syncs their state to React.
 */

import { useEffect, useCallback, useRef } from 'react'
import { useAppDispatch } from '../context'

// Import existing controllers (singleton instances)
import { voiceController, type VoiceEvent } from '../../lib/voice-controller'
import { audioController } from '../../lib/audio-controller'
import { appController } from '../../lib/app-controller'
import { sessionHandler } from '../../lib/session-handler'
import { conversationController } from '../../lib/conversation-controller'
import { stateManager, type StateChangeEvent } from '../../lib/state-manager'

export interface UseRealtimeSessionOptions {
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

  // Connect to realtime session
  const connect = useCallback(async () => {
    try {
      dispatch({ type: 'SET_VOICE_STATUS', status: 'connecting' })
      await appController.connect()
      dispatch({ type: 'SET_CONNECTED', connected: true })
      options.onConnected?.()
    } catch (error) {
      console.error('[useRealtimeSession] Connect failed:', error)
      dispatch({ type: 'SET_VOICE_STATUS', status: 'error' })
      options.onError?.(error as Error)
    }
  }, [dispatch, options])

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

  // PTT press handler
  const handlePTTPress = useCallback(() => {
    voiceController.startPTT()
  }, [])

  // PTT release handler
  const handlePTTRelease = useCallback(() => {
    voiceController.stopPTT()
  }, [])

  // Toggle hands-free mode
  const toggleHandsFree = useCallback(() => {
    voiceController.setHandsFree(!voiceController.getState().handsFree)
  }, [])

  return {
    // Actions
    connect,
    disconnect,
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
