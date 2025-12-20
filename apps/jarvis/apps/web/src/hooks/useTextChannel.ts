/**
 * useTextChannel hook - Text message sending
 *
 * This hook manages sending text messages to the assistant.
 * Uses appController.sendText() for backend communication.
 */

import { useCallback, useState } from 'react'
import { useAppState, useAppDispatch, type ChatMessage } from '../context'
import { appController } from '../../lib/app-controller'
import { uuid } from '../../lib/uuid'

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

      const correlationId = uuid()

      // Create user message
      const userMessage: ChatMessage = {
        id: uuid(),
        role: 'user',
        content: trimmedText,
        timestamp: new Date(),
      }

      // Create assistant placeholder (queued)
      const assistantPlaceholder: ChatMessage = {
        id: uuid(),
        role: 'assistant',
        content: '',
        status: 'queued',
        timestamp: new Date(),
        correlationId,
      }

      // Add to messages
      dispatch({ type: 'ADD_MESSAGE', message: userMessage })
      dispatch({ type: 'ADD_MESSAGE', message: assistantPlaceholder })
      options.onMessageSent?.(userMessage)

      // Clear any previous error
      setLastError(null)

      try {
        console.log('[useTextChannel] Sending message:', trimmedText, 'correlationId:', correlationId)

        // Send to backend via appController
        await appController.sendText(trimmedText, correlationId)
        // Response arrives via Supervisor SSE -> stateManager -> React
        setIsSending(false)
      } catch (error) {
        console.error('[useTextChannel] Error sending message:', error)

        // Update assistant bubble to error state
        dispatch({
          type: 'UPDATE_MESSAGE_BY_CORRELATION_ID',
          correlationId,
          updates: { status: 'error' },
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
