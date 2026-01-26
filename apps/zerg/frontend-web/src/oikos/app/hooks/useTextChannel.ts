/**
 * useTextChannel hook - Text message sending
 *
 * This hook manages sending text messages to the assistant.
 * Uses OikosChatController for backend communication.
 */

import { useCallback, useState, useRef, useEffect } from 'react'
import { useAppState, useAppDispatch, type ChatMessage } from '../context'
import { OikosChatController } from '../../lib/oikos-chat-controller'
import { uuid } from '../../lib/uuid'
import { logger } from '../../core'
import { eventBus } from '../../lib/event-bus'
import { timelineLogger } from '../../lib/timeline-logger'

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
  const sendCounterRef = useRef(0)
  const optionsRef = useRef(options)
  optionsRef.current = options

  // Initialize oikos chat controller
  const oikosChatRef = useRef<OikosChatController | null>(null)
  const initRef = useRef(false)

  useEffect(() => {
    if (initRef.current) return
    initRef.current = true

    const controller = new OikosChatController({ maxRetries: 3 })
    controller.initialize().then(() => {
      oikosChatRef.current = controller
      logger.info('[useTextChannel] OikosChatController initialized')
    }).catch((error) => {
      logger.error('[useTextChannel] Failed to initialize OikosChatController:', error)
    })
  }, [])

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

      if (!oikosChatRef.current) {
        const err = new Error('Chat not initialized')
        setLastError(err)
        optionsRef.current.onError?.(err)
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
      optionsRef.current.onMessageSent?.(userMessage)

      // Clear any previous error
      setLastError(null)

      try {
        logger.info(`[useTextChannel] Sending message, messageId: ${messageId}`)

        // Set message ID for timeline tracking
        timelineLogger.setMessageId(messageId)

        // Emit text_channel:sent event for timeline tracking
        eventBus.emit('text_channel:sent', {
          text: trimmedText,
          timestamp: Date.now(),
        })

        // Yield to browser to ensure placeholder renders before SSE events arrive
        // This prevents React batching from skipping the typing indicator
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))

        // Send to backend via OikosChatController
        await oikosChatRef.current.sendMessage(trimmedText, messageId, {
          model: preferences.chat_model,
          reasoning_effort: preferences.reasoning_effort,
        })
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
        optionsRef.current.onError?.(err)
      } finally {
        if (sendCounterRef.current === sendId) {
          // Response arrives via Oikos SSE events; keep input unlocked after completion.
          setIsSending(false)
        }
      }
    },
    [dispatch, preferences]
  )

  useEffect(() => {
    const unsubscribe = eventBus.on('text_channel:send', (data) => {
      void sendMessage(data.text)
    })

    return () => unsubscribe()
  }, [sendMessage])

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
