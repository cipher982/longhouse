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

  const formatTokens = (n?: number | null) => {
    if (n === null || n === undefined) return null
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 10_000) return `${Math.round(n / 1000)}k`
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
    return `${n}`
  }

  const buildUsageTitle = (usage?: ChatMessage['usage']) => {
    if (!usage) return null

    const total = usage.total_tokens
    const prompt = usage.prompt_tokens
    const completion = usage.completion_tokens
    const reasoning = usage.reasoning_tokens

    const lines: string[] = []
    if (total !== null && total !== undefined) lines.push(`Run tokens (input+output): ${total.toLocaleString()}`)
    if (prompt !== null && prompt !== undefined) lines.push(`Input tokens: ${prompt.toLocaleString()}`)
    if (completion !== null && completion !== undefined) lines.push(`Output tokens: ${completion.toLocaleString()}`)
    if (reasoning !== null && reasoning !== undefined && reasoning > 0) lines.push(`Reasoning tokens: ${reasoning.toLocaleString()} (subset of output)`)

    return lines.length ? lines.join('\n') : null
  }

  const buildUsageLine = (usage?: ChatMessage['usage']) => {
    if (!usage) return null

    const total = formatTokens(usage.total_tokens)
    if (!total) return null

    const reasoning = usage.reasoning_tokens
    const reasoningPart = reasoning && reasoning > 0 ? ` · 🧠 ${formatTokens(reasoning)}` : ''
    return `Run ${total}${reasoningPart}`
  }

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
              const usageTitle = isAssistant ? buildUsageTitle(message.usage) : null
              const usageLine = isAssistant ? buildUsageLine(message.usage) : null

              return (
                <div
                  key={message.id}
                  className={`message ${message.role}${message.skipAnimation ? ' no-animate' : ''}${isQueued ? ' queued' : ''}`}
                >
                  <div className="message-bubble" tabIndex={isAssistant && usageTitle && usageLine ? 0 : undefined}>
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
                    {isAssistant && usageTitle && usageLine && (
                      <div className="message-usage" aria-hidden="true">
                        <span className="message-usage-text" title={usageTitle}>
                          {usageLine}
                        </span>
                      </div>
                    )}
                  </div>
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
