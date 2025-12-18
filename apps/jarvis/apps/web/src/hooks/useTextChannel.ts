/**
 * useTextChannel hook - Text message sending
 *
 * This hook manages sending text messages to the assistant.
 * Uses appController.sendText() for backend communication.
 */

import { useCallback, useState } from 'react'
import { useAppState, useAppDispatch, type ChatMessage } from '../context'
import { appController } from '../../lib/app-controller'

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

        // Send to backend via appController
        await appController.sendText(trimmedText)
        // Response arrives via Supervisor SSE -> stateManager -> React
        setIsSending(false)
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
