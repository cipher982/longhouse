/**
 * useTextChannel hook - Text message sending
 *
 * This hook manages sending text messages to the assistant.
 * When bridge mode is enabled, uses appController.sendText() for real backend communication.
 * When standalone mode is enabled, simulates responses (development only).
 */

import { useCallback, useState } from 'react'
import { useAppState, useAppDispatch, type ChatMessage } from '../context'

// Feature flag - must match App.tsx
const ENABLE_REALTIME_BRIDGE = import.meta.env.VITE_JARVIS_ENABLE_REALTIME_BRIDGE === 'true'

// Lazy import for bridge mode to avoid loading legacy code in standalone mode
let appControllerPromise: Promise<typeof import('../../lib/app-controller')> | null = null
function getAppController() {
  if (!appControllerPromise) {
    appControllerPromise = import('../../lib/app-controller')
  }
  return appControllerPromise
}

export interface UseTextChannelOptions {
  onMessageSent?: (message: ChatMessage) => void
  onResponse?: (message: ChatMessage) => void
  onError?: (error: Error) => void
}

export function useTextChannel(options: UseTextChannelOptions = {}) {
  const state = useAppState()
  const dispatch = useAppDispatch()
  const [isSending, setIsSending] = useState(false)
  const [lastError, setLastError] = useState<Error | null>(null)

  const { messages, streamingContent, isConnected } = state

  // Clear error state
  const clearError = useCallback(() => {
    setLastError(null)
  }, [])

  // Send a text message
  const sendMessage = useCallback(
    async (text: string) => {
      if (!text.trim()) {
        return
      }

      const trimmedText = text.trim()
      setIsSending(true)

      // Create user message
      const userMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'user',
        content: trimmedText,
        timestamp: new Date(),
      }

      // Add to messages
      dispatch({ type: 'ADD_MESSAGE', message: userMessage })
      options.onMessageSent?.(userMessage)

      // Clear any previous error
      setLastError(null)

      try {
        console.log('[useTextChannel] Sending message:', trimmedText)

        if (ENABLE_REALTIME_BRIDGE) {
          // Bridge mode: Use appController to send to real backend
          const { appController } = await getAppController()
          await appController.sendText(trimmedText)
          // Response will come through realtime session events
          // The conversation controller handles adding assistant messages
          setIsSending(false)
        } else {
          // Standalone mode: Simulate response (development/testing only)
          console.warn(
            '[useTextChannel] Running in standalone mode - responses are simulated. ' +
            'Set VITE_JARVIS_ENABLE_REALTIME_BRIDGE=true for real backend integration.'
          )

          dispatch({ type: 'SET_STREAMING_CONTENT', content: '' })

          setTimeout(() => {
            const assistantMessage: ChatMessage = {
              id: crypto.randomUUID(),
              role: 'assistant',
              content: `[Standalone mode] Received: "${trimmedText}"\n\nTo enable real responses, set VITE_JARVIS_ENABLE_REALTIME_BRIDGE=true`,
              timestamp: new Date(),
            }
            dispatch({ type: 'ADD_MESSAGE', message: assistantMessage })
            dispatch({ type: 'SET_STREAMING_CONTENT', content: '' })
            options.onResponse?.(assistantMessage)
            setIsSending(false)
          }, 500)
        }
      } catch (error) {
        console.error('[useTextChannel] Error sending message:', error)

        // Rollback: Remove the optimistic user message on failure
        dispatch({
          type: 'SET_MESSAGES',
          messages: messages.filter((m) => m.id !== userMessage.id),
        })

        dispatch({ type: 'SET_STREAMING_CONTENT', content: '' })

        // Surface error to UI
        const err = error as Error
        setLastError(err)
        options.onError?.(err)
        setIsSending(false)
      }
    },
    [dispatch, options, messages]
  )

  // Clear all messages
  const clearMessages = useCallback(() => {
    dispatch({ type: 'SET_MESSAGES', messages: [] })
    dispatch({ type: 'SET_STREAMING_CONTENT', content: '' })
  }, [dispatch])

  return {
    // State
    messages,
    streamingContent,
    isStreaming: streamingContent.length > 0,
    isSending,
    isConnected,
    lastError,

    // Actions
    sendMessage,
    clearMessages,
    clearError,
  }
}
