/**
 * ChatContainer component - Message display area
 */

import { useEffect, useRef, useSyncExternalStore } from 'react'
import { renderMarkdown } from '../../lib/markdown-renderer'
import { supervisorToolStore } from '../../lib/supervisor-tool-store'
import { ActivityStream } from './ActivityStream'
import type { ChatMessage } from '../context/types'

interface ChatContainerProps {
  messages: ChatMessage[]
  userTranscriptPreview?: string  // Live voice transcript preview
}

export function ChatContainer({ messages, userTranscriptPreview }: ChatContainerProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  // Subscribe to supervisor tool store for activity stream
  const toolState = useSyncExternalStore(
    supervisorToolStore.subscribe.bind(supervisorToolStore),
    () => supervisorToolStore.getState()
  )

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
    const reasoningPart = reasoning && reasoning > 0 ? ` · Reasoning ${formatTokens(reasoning)}` : ''
    return `Run ${total}${reasoningPart}`
  }

  // Auto-scroll to bottom when new messages arrive or during streaming
  // Note: Don't include toolState here - it updates frequently (ticker, status changes)
  // and would cause scroll jumps when user is trying to interact with tool cards
  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [messages, userTranscriptPreview])

  // Scroll when new tools are added (but not on every status update)
  const toolCount = toolState.tools.size
  useEffect(() => {
    if (containerRef.current && toolCount > 0) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [toolCount])

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
              const hasContent = message.content && message.content.length > 0;
              // Show typing dots for any pending assistant message without content
              // This handles React batching where status jumps from queued -> streaming instantly
              const isPending = isAssistant && message.status !== 'final' && message.status !== 'error' && message.status !== 'canceled';
              const showTypingDots = isPending && !hasContent;
              const usageTitle = isAssistant ? buildUsageTitle(message.usage) : null
              const usageLine = isAssistant ? buildUsageLine(message.usage) : null

              // For any assistant message with a runId, render tools inline before the message
              // This works for both pending (streaming) and finalized messages
              const hasRunId = isAssistant && message.runId;
              const inlineTools = hasRunId ? supervisorToolStore.getToolsForRun(message.runId!) : [];

              return (
                <div key={message.id} className="message-group">
                  {/* Render completed tools BEFORE the assistant response */}
                  {inlineTools.length > 0 && (
                    <ActivityStream runId={message.runId!} />
                  )}
                  <div
                    className={`message ${message.role}${message.skipAnimation ? ' no-animate' : ''}${showTypingDots ? ' typing' : ''}`}
                  >
                    <div className="message-bubble" tabIndex={isAssistant && usageTitle && usageLine ? 0 : undefined}>
                      <div className="message-content">
                        {showTypingDots ? (
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
                </div>
              );
            })}
            {/* Show supervisor tool activity stream only if no message is associated with this run yet */}
            {toolState.currentRunId && toolState.isActive && !messages.some(m => m.runId === toolState.currentRunId) && (
              <ActivityStream runId={toolState.currentRunId} />
            )}
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
