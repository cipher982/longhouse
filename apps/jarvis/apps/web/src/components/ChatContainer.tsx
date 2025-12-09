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
  userTranscriptPreview?: string  // Live voice transcript preview
}

export function ChatContainer({ messages, isStreaming, streamingContent, userTranscriptPreview }: ChatContainerProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to bottom when new messages arrive or during streaming
  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [messages, streamingContent, userTranscriptPreview])

  const hasContent = messages.length > 0 || isStreaming || userTranscriptPreview

  return (
    <div className="chat-wrapper">
      <div className="transcript" ref={containerRef}>
        {!hasContent ? (
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
            {/* Show live user voice transcript preview */}
            {userTranscriptPreview && (
              <div className="message user preview">
                <div className="message-content">{userTranscriptPreview}</div>
              </div>
            )}
            {/* Show assistant streaming response */}
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
