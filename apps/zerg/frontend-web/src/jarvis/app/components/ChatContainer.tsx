/**
 * ChatContainer component - Message display area
 *
 * Uses timeline-based rendering: messages and tools are both events
 * sorted by timestamp and rendered in chronological order.
 * Each event renders exactly once - no duplication possible.
 */

import { useEffect, useRef, useMemo, useSyncExternalStore } from 'react'
import { renderMarkdown } from '../../lib/markdown-renderer'
import { conciergeToolStore, type ConciergeToolCall } from '../../lib/concierge-tool-store'
import { ToolCard } from './ToolCard'
import { CommisToolCard } from './CommisToolCard'
import type { ChatMessage } from '../context/types'

// Timeline event types - messages and tools are both events in the conversation
// sortOrder is used as a tie-breaker when timestamps are equal:
//   0 = user message (first)
//   1 = tool (middle)
//   2 = assistant message (last)
// This ensures: user msg → tools → assistant response within the same course
type TimelineEvent =
  | { type: 'message'; id: string; timestamp: number; sortOrder: number; courseId: number; data: ChatMessage }
  | { type: 'tool'; id: string; timestamp: number; sortOrder: number; courseId: number; data: ConciergeToolCall }

interface ChatContainerProps {
  messages: ChatMessage[]
  userTranscriptPreview?: string  // Live voice transcript preview
}

export function ChatContainer({ messages, userTranscriptPreview }: ChatContainerProps) {
  // Ref on wrapper (scroll container) - scrolling now happens on outer element
  const wrapperRef = useRef<HTMLDivElement>(null)

  // Subscribe to concierge tool store
  const toolState = useSyncExternalStore(
    conciergeToolStore.subscribe.bind(conciergeToolStore),
    () => conciergeToolStore.getState()
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
    if (total !== null && total !== undefined) lines.push(`Course tokens (input+output): ${total.toLocaleString()}`)
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
    return `Course ${total}${reasoningPart}`
  }

  // Auto-scroll to bottom when new messages arrive or during streaming
  // Note: Don't include toolState here - it updates frequently (ticker, status changes)
  // and would cause scroll jumps when user is trying to interact with tool cards
  useEffect(() => {
    if (wrapperRef.current) {
      wrapperRef.current.scrollTop = wrapperRef.current.scrollHeight
    }
  }, [messages, userTranscriptPreview])

  // Scroll when new tools are added (but not on every status update)
  const toolCount = toolState.tools.size
  useEffect(() => {
    if (wrapperRef.current && toolCount > 0) {
      wrapperRef.current.scrollTop = wrapperRef.current.scrollHeight
    }
  }, [toolCount])

  // Build unified timeline of messages and tools
  // Sort by courseId first (to keep related items together), then by logical order
  // This ensures: user message → tools → assistant response within each course
  const timeline = useMemo((): TimelineEvent[] => {
    const events: TimelineEvent[] = []

    // Add messages with sortOrder for logical ordering within a course
    // User messages sort first (0), assistant messages sort last (2)
    for (const msg of messages) {
      const timestamp = msg.timestamp?.getTime()
      events.push({
        type: 'message',
        id: msg.id,
        // Use timestamp if valid, otherwise Infinity to put at end
        timestamp: timestamp && Number.isFinite(timestamp) ? timestamp : Infinity,
        // User=0 (first), Assistant=2 (last)
        sortOrder: msg.role === 'user' ? 0 : 2,
        // courseId for grouping (0 if not set)
        courseId: msg.courseId ?? 0,
        data: msg,
      })
    }

    // Add tools with sortOrder=1 (middle, between user and assistant)
    for (const tool of toolState.tools.values()) {
      events.push({
        type: 'tool',
        id: tool.toolCallId,
        timestamp: Number.isFinite(tool.startedAt) ? tool.startedAt : Infinity,
        sortOrder: 1,
        courseId: tool.courseId ?? 0,
        data: tool,
      })
    }

    // Sort strategy:
    // 1. Different courseIds: sort by timestamp (chronological order of courses)
    // 2. Same courseId: sort by sortOrder (user → tools → assistant)
    // This fixes the visual "jumping" issue where tools appeared after assistant
    return events.sort((a, b) => {
      const stableIdDiff = a.id.localeCompare(b.id)

      // If same course, use logical ordering
      if (a.courseId !== 0 && b.courseId !== 0 && a.courseId === b.courseId) {
        const orderDiff = a.sortOrder - b.sortOrder
        if (orderDiff !== 0) return orderDiff

        // Same sortOrder (e.g., multiple tools): sort chronologically, then stably by id
        const timeDiff = a.timestamp - b.timestamp
        if (timeDiff !== 0) return timeDiff
        return stableIdDiff
      }

      // Different courses (or courseId=0): sort by timestamp
      const timeDiff = a.timestamp - b.timestamp
      if (timeDiff !== 0) return timeDiff

      // Same timestamp across courses: preserve logical order, then stably by id
      const orderDiff = a.sortOrder - b.sortOrder
      if (orderDiff !== 0) return orderDiff
      return stableIdDiff
    })
  }, [messages, toolState.tools])

  // Check if any commis are actively running (for hiding typing dots)
  const hasActiveCommis = useMemo(() => {
    return Array.from(toolState.tools.values()).some(tool => {
      if (tool.toolName === 'spawn_commis') {
        const commisStatus = (tool.result as Record<string, unknown>)?.commisStatus
        return commisStatus === 'running' || commisStatus === 'spawned'
      }
      return tool.status === 'running'
    })
  }, [toolState.tools])

  const hasContent = messages.length > 0 || toolState.tools.size > 0 || userTranscriptPreview

  // Render a tool event
  const renderTool = (tool: ConciergeToolCall) => {
    if (tool.toolName === 'spawn_commis') {
      const isDeferred = conciergeToolStore.isDeferred(tool.courseId)
      const commisStatus = (tool.result as Record<string, unknown>)?.commisStatus
      const isDetached = isDeferred && (commisStatus === 'running' || commisStatus === 'spawned')
      return <CommisToolCard key={tool.toolCallId} tool={tool} isDetached={isDetached} detachedIndex={0} />
    }
    return <ToolCard key={tool.toolCallId} tool={tool} />
  }

  // Render a message event
  const renderMessage = (message: ChatMessage) => {
    const isAssistant = message.role === 'assistant'
    const hasMessageContent = message.content && message.content.length > 0
    const isPending = isAssistant && message.status !== 'final' && message.status !== 'error' && message.status !== 'canceled'

    // Hide thinking dots if commis are showing progress
    const showTypingDots = isPending && !hasMessageContent && !hasActiveCommis
    const usageTitle = isAssistant ? buildUsageTitle(message.usage) : null
    const usageLine = isAssistant ? buildUsageLine(message.usage) : null

    return (
      <div key={message.id} className="message-group">
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
    )
  }

  return (
    <div className="chat-wrapper" ref={wrapperRef}>
      <div className="transcript" data-testid="messages-container">
        {!hasContent ? (
          <div className="status-message">
            <div className="status-text">System Ready</div>
            <div className="status-subtext">Tap the microphone or type a message to begin</div>
          </div>
        ) : (
          <>
            {/* Render timeline events in chronological order */}
            {timeline.map(event =>
              event.type === 'tool'
                ? renderTool(event.data)
                : renderMessage(event.data)
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
