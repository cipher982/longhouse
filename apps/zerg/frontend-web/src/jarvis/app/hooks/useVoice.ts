/**
 * useVoice hook - Voice state and PTT/VAD handling
 *
 * This hook wraps voice functionality and integrates with React state.
 * Eventually will integrate with OpenAI realtime session.
 */

import { useCallback, useRef, useEffect } from 'react'
import { useAppState, useAppDispatch } from '../context'

export interface UseVoiceOptions {
  onTranscript?: (text: string, isFinal: boolean) => void
  onError?: (error: Error) => void
}

export function useVoice(options: UseVoiceOptions = {}) {
  const state = useAppState()
  const dispatch = useAppDispatch()
  const micStreamRef = useRef<MediaStream | null>(null)

  const { voiceMode, voiceStatus, isConnected } = state

  // Request microphone access
  const requestMicAccess = useCallback(async () => {
    try {
      if (micStreamRef.current) {
        return micStreamRef.current
      }

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })

      micStreamRef.current = stream
      dispatch({ type: 'SET_MIC_STREAM', stream })
      return stream
    } catch (error) {
      console.error('Failed to get microphone access:', error)
      dispatch({ type: 'SET_VOICE_STATUS', status: 'error' })
      options.onError?.(error as Error)
      return null
    }
  }, [dispatch, options])

  // Release microphone
  const releaseMic = useCallback(() => {
    if (micStreamRef.current) {
      micStreamRef.current.getTracks().forEach((track) => track.stop())
      micStreamRef.current = null
      dispatch({ type: 'SET_MIC_STREAM', stream: null })
    }
  }, [dispatch])

  // Start listening (PTT press or VAD activation)
  const startListening = useCallback(async () => {
    console.log('[useVoice] startListening')

    const stream = await requestMicAccess()
    if (!stream) return

    dispatch({ type: 'SET_VOICE_STATUS', status: 'listening' })

    // TODO: Connect to OpenAI realtime session when available
    // For now, simulate listening state
  }, [dispatch, requestMicAccess])

  // Stop listening (PTT release or VAD deactivation)
  const stopListening = useCallback(() => {
    console.log('[useVoice] stopListening')
    dispatch({ type: 'SET_VOICE_STATUS', status: 'processing' })

    // TODO: Send audio to OpenAI realtime session
    // For now, simulate processing -> ready transition
    setTimeout(() => {
      dispatch({ type: 'SET_VOICE_STATUS', status: 'idle' })
    }, 500)
  }, [dispatch])

  // Toggle voice mode (PTT <-> Hands-free)
  const toggleMode = useCallback(() => {
    const newMode = voiceMode === 'push-to-talk' ? 'hands-free' : 'push-to-talk'
    dispatch({ type: 'SET_VOICE_MODE', mode: newMode })
    console.log('[useVoice] Mode changed to:', newMode)
  }, [dispatch, voiceMode])

  // Handle PTT button press
  const handlePTTPress = useCallback(() => {
    if (voiceMode === 'push-to-talk') {
      startListening()
    }
  }, [voiceMode, startListening])

  // Handle PTT button release
  const handlePTTRelease = useCallback(() => {
    if (voiceMode === 'push-to-talk' && voiceStatus === 'listening') {
      stopListening()
    }
  }, [voiceMode, voiceStatus, stopListening])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      releaseMic()
    }
  }, [releaseMic])

  return {
    // State
    mode: voiceMode,
    status: voiceStatus,
    isConnected,
    isListening: voiceStatus === 'listening',
    isProcessing: voiceStatus === 'processing',
    isSpeaking: voiceStatus === 'speaking',

    // Actions
    toggleMode,
    startListening,
    stopListening,
    handlePTTPress,
    handlePTTRelease,
    requestMicAccess,
    releaseMic,
  }
}
