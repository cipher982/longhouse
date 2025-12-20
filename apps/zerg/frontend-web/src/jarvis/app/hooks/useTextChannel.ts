/**
 * useTextChannel hook - Text message sending
 *
 * This hook manages sending text messages to the assistant.
 * Uses SupervisorChatController for backend communication.
 */

import { useCallback, useState, useRef, useEffect } from 'react'
import { useAppState, useAppDispatch, type ChatMessage } from '../context'
import { SupervisorChatController } from '../../lib/supervisor-chat-controller'
import { uuid } from '../../lib/uuid'
import { logger } from '../../core'

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
  const optionsRef = useRef(options)
  optionsRef.current = options

  // Initialize supervisor chat controller
  const supervisorChatRef = useRef<SupervisorChatController | null>(null)
  const initRef = useRef(false)

  useEffect(() => {
    if (initRef.current) return
    initRef.current = true

    const controller = new SupervisorChatController({ maxRetries: 3 })
    controller.initialize().then(() => {
      supervisorChatRef.current = controller
      logger.info('[useTextChannel] SupervisorChatController initialized')
    }).catch((error) => {
      logger.error('[useTextChannel] Failed to initialize SupervisorChatController:', error)
    })
  }, [])

  const { messages, streamingContent, isConnected, preferences } = state

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

      if (!supervisorChatRef.current) {
        const err = new Error('Chat not initialized')
        setLastError(err)
        optionsRef.current.onError?.(err)
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
      optionsRef.current.onMessageSent?.(userMessage)

      // Clear any previous error
      setLastError(null)

      try {
        logger.info(`[useTextChannel] Sending message, correlationId: ${correlationId}`)

        // Send to backend via SupervisorChatController
        await supervisorChatRef.current.sendMessage(trimmedText, correlationId, {
          model: preferences.chat_model,
          reasoning_effort: preferences.reasoning_effort,
        })
        // Response arrives via Supervisor SSE events
        setIsSending(false)
      } catch (error) {
        logger.error('[useTextChannel] Error sending message:', error)

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
        optionsRef.current.onError?.(err)
        setIsSending(false)
      }
    },
    [dispatch, preferences]
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
