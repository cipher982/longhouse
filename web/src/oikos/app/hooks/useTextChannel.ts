/**
 * useTextChannel hook - Text message sending
 *
 * This hook manages optimistic text-message UI state.
 * The actual chat controller lives in useOikosApp.
 */

import { useCallback, useState, useRef, useEffect } from 'react'
import { useAppState, useAppDispatch, type ChatMessage } from '../context'
import { uuid } from '../../lib/uuid'
import { logger } from '../../core'
import { eventBus } from '../../lib/event-bus'

interface UseTextChannelOptions {
  sendText: (text: string, messageId: string) => Promise<void>
}

export function useTextChannel({ sendText }: UseTextChannelOptions) {
  const state = useAppState()
  const dispatch = useAppDispatch()
  const [isSending, setIsSending] = useState(false)
  const [lastError, setLastError] = useState<Error | null>(null)
  const sendCounterRef = useRef(0)

  useEffect(() => {
    const clearSending = () => setIsSending(false)
    const unsubComplete = eventBus.on('oikos:complete', clearSending)
    const unsubDeferred = eventBus.on('oikos:deferred', clearSending)
    const unsubError = eventBus.on('oikos:error', clearSending)

    return () => {
      unsubComplete()
      unsubDeferred()
      unsubError()
    }
  }, [])

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
      const sendId = ++sendCounterRef.current
      setIsSending(true)

      // Generate messageId upfront (client-generated, no binding step needed)
      const messageId = uuid()

      // Create user message
      const userMessage: ChatMessage = {
        id: uuid(),
        role: 'user',
        content: trimmedText,
        timestamp: new Date(),
      }

      // Create assistant placeholder with messageId (no separate correlationId)
      const assistantPlaceholder: ChatMessage = {
        id: uuid(),
        role: 'assistant',
        content: '',
        status: 'queued',
        timestamp: new Date(),
        messageId,  // Use messageId directly
      }

      // Add to messages
      dispatch({ type: 'ADD_MESSAGE', message: userMessage })
      dispatch({ type: 'ADD_MESSAGE', message: assistantPlaceholder })

      // Clear any previous error
      setLastError(null)

      try {
        logger.info(`[useTextChannel] Sending message, messageId: ${messageId}`)

        // Yield to browser to ensure placeholder renders before SSE events arrive
        // This prevents React batching from skipping the typing indicator
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))

        await sendText(trimmedText, messageId)
      } catch (error) {
        logger.error('[useTextChannel] Error sending message:', error)

        // Update assistant bubble to error state
        dispatch({
          type: 'UPDATE_MESSAGE_BY_MESSAGE_ID',
          messageId,
          updates: { status: 'error' },
        })

        dispatch({ type: 'SET_STREAMING_CONTENT', content: '' })

        // Surface error to UI
        const err = error as Error
        setLastError(err)
      } finally {
        if (sendCounterRef.current === sendId) {
          // Response arrives via Oikos SSE events; keep input unlocked after completion.
          setIsSending(false)
        }
      }
    },
    [dispatch, sendText]
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
