/**
 * ChatContainer component - Message display area
 */

import { useEffect, useRef } from 'react'
import { renderMarkdown } from '../../lib/markdown-renderer'
import type { ChatMessage } from '../context/types'

interface ChatContainerProps {
  messages: ChatMessage[]
  userTranscriptPreview?: string  // Live voice transcript preview
}

export function ChatContainer({ messages, userTranscriptPreview }: ChatContainerProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to bottom when new messages arrive or during streaming
  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [messages, userTranscriptPreview])

  const hasContent = messages.length > 0 || userTranscriptPreview

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
            {messages.map((message) => {
              const isAssistant = message.role === 'assistant';
              const isQueued = isAssistant && message.status === 'queued';
              const isTyping = isAssistant && message.status === 'typing';
              const hasContent = message.content && message.content.length > 0;

              return (
                <div
                  key={message.id}
                  className={`message ${message.role}${message.skipAnimation ? ' no-animate' : ''}${isQueued ? ' queued' : ''}`}
                >
                  <div className="message-content">
                    {isQueued && !hasContent ? (
                      <div className="placeholder-shimmer" style={{ width: '100px', height: '20px', borderRadius: '4px', background: 'rgba(255,255,255,0.1)' }} />
                    ) : isTyping && !hasContent ? (
                      <div className="thinking-dots thinking-dots--in-chat">
                        <span className="thinking-dot"></span>
                        <span className="thinking-dot"></span>
                        <span className="thinking-dot"></span>
                      </div>
                    ) : (
                      <div dangerouslySetInnerHTML={{ __html: renderMarkdown(message.content) }} />
                    )}
                  </div>
                  {/* Debug mode: show token usage when reasoning tokens present */}
                  {isAssistant && message.usage && message.usage.reasoning_tokens && message.usage.reasoning_tokens > 0 && (
                    <div className="message-debug-info">
                      <span className="debug-badge">🧠 {message.usage.reasoning_tokens} reasoning tokens</span>
                      <span className="debug-detail">({message.usage.total_tokens} total)</span>
                    </div>
                  )}
                </div>
              );
            })}
            {/* Show live user voice transcript preview */}
            {userTranscriptPreview && (
              <div className="message user preview">
                <div className="message-content">{userTranscriptPreview}</div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
