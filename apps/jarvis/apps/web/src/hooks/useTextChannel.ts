/**
 * useTextChannel hook - Text message sending
 *
 * This hook manages sending text messages to the assistant.
 * Integrates with the Jarvis client for backend communication.
 */

import { useCallback, useState } from 'react'
import { useAppState, useAppDispatch, type ChatMessage } from '../context'

export interface UseTextChannelOptions {
  onMessageSent?: (message: ChatMessage) => void
  onResponse?: (message: ChatMessage) => void
  onError?: (error: Error) => void
}

export function useTextChannel(options: UseTextChannelOptions = {}) {
  const state = useAppState()
  const dispatch = useAppDispatch()
  const [isSending, setIsSending] = useState(false)

  const { messages, streamingContent, isConnected } = state

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

      try {
        // TODO: Send to Jarvis backend via realtime session or HTTP
        // For now, simulate a response
        console.log('[useTextChannel] Sending message:', trimmedText)

        // Simulate streaming response
        dispatch({ type: 'SET_STREAMING_CONTENT', content: '' })

        // Simulate assistant response after delay
        setTimeout(() => {
          const assistantMessage: ChatMessage = {
            id: crypto.randomUUID(),
            role: 'assistant',
            content: `I received your message: "${trimmedText}". (React migration in progress - real responses coming soon!)`,
            timestamp: new Date(),
          }
          dispatch({ type: 'ADD_MESSAGE', message: assistantMessage })
          dispatch({ type: 'SET_STREAMING_CONTENT', content: '' })
          options.onResponse?.(assistantMessage)
          setIsSending(false)
        }, 1000)
      } catch (error) {
        console.error('[useTextChannel] Error sending message:', error)
        dispatch({ type: 'SET_STREAMING_CONTENT', content: '' })
        options.onError?.(error as Error)
        setIsSending(false)
      }
    },
    [dispatch, options]
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

    // Actions
    sendMessage,
    clearMessages,
  }
}
