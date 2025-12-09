/**
 * ChatContainer component - Message display area
 */

import { useEffect, useRef } from 'react'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp?: Date
  isStreaming?: boolean
}

interface ChatContainerProps {
  messages: ChatMessage[]
  isStreaming?: boolean
  streamingContent?: string
}

export function ChatContainer({ messages, isStreaming, streamingContent }: ChatContainerProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [messages, streamingContent])

  return (
    <div className="chat-container">
      <div className="transcript" ref={containerRef}>
        {messages.length === 0 && !isStreaming ? (
          <div className="status-message">
            <div className="status-text">System Ready</div>
            <div className="status-subtext">Tap the microphone or type a message to begin</div>
          </div>
        ) : (
          <>
            {messages.map((message) => (
              <div key={message.id} className={`message ${message.role}`}>
                <div className="message-content">{message.content}</div>
              </div>
            ))}
            {isStreaming && streamingContent && (
              <div className="message assistant streaming">
                <div className="message-content">{streamingContent}</div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
