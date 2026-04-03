/**
 * DebugPanel - Dev/Admin sidebar with high-signal debug info
 * Only visible to developers (isDevelopment) or admins (user.role === 'ADMIN')
 */

import { useQuery } from '@tanstack/react-query'
import { useAppState } from '../context'
import config from '../../../lib/config'
import { fetchWithRefresh } from '../../../lib/auth-refresh'
import './DebugPanel.css'

interface ThreadInfo {
  thread_id: number
  title: string
  message_count: number
  canonical_conversation?: {
    id: number
    kind: string
    title?: string | null
    external_conversation_id: string
    message_count: number
  }
}

interface DebugPanelProps {
  isOpen: boolean
  onToggle: () => void
  onReset: () => void
  isResetting?: boolean
}

async function fetchThreadInfo(): Promise<ThreadInfo | null> {
  try {
    const response = await fetchWithRefresh(`${config.apiBaseUrl}/oikos/thread`, {
      credentials: 'include',
    })
    if (!response.ok) {
      return null
    }
    return response.json()
  } catch (error) {
    console.warn('[DebugPanel] Failed to fetch thread info:', error)
    return null
  }
}

export function DebugPanel({ isOpen, onToggle, onReset, isResetting = false }: DebugPanelProps) {
  const state = useAppState()
  const threadInfoQuery = useQuery({
    queryKey: ['oikos-thread-info', state.messages.length],
    queryFn: fetchThreadInfo,
    placeholderData: (previousData) => previousData,
    refetchInterval: 10000,
    retry: false,
  })
  const threadInfo = threadInfoQuery.data ?? null

  const handleReset = async () => {
    await onReset()
    await threadInfoQuery.refetch()
  }

  const voiceStatusColor = {
    idle: 'var(--color-text-muted)',
    connecting: 'var(--color-intent-warning)',
    ready: 'var(--color-intent-success)',
    listening: 'var(--color-neon-primary)',
    processing: 'var(--color-intent-warning)',
    speaking: 'var(--color-neon-secondary)',
    error: 'var(--color-intent-error)',
  }

  return (
    <>
      {/* Mobile Menu Toggle */}
      <button
        id="sidebarToggle"
        className="sidebar-toggle"
        type="button"
        aria-label="Toggle debug panel"
        aria-expanded={isOpen}
        onClick={onToggle}
      >
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />
        </svg>
      </button>

      {/* Debug Panel */}
      <div className={`sidebar debug-panel ${isOpen ? 'open' : ''}`}>
        <div className="sidebar-header">
          <h2>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: '8px' }}>
              <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />
            </svg>
            Debug
          </h2>
        </div>

        <div className="sidebar-content">
          {/* Reset Button */}
          <button
            className="sidebar-button danger"
            onClick={handleReset}
            disabled={isResetting}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14zM10 11v6M14 11v6" />
            </svg>
            {isResetting ? 'Resetting...' : 'Reset Memory'}
          </button>

          {/* Thread Info */}
          <div className="debug-section">
            <div className="debug-section-header">Thread</div>
            <div className="debug-row">
              <span className="debug-label">ID</span>
              <span className="debug-value" data-testid="debug-thread-id">
                {threadInfo?.thread_id ?? '—'}
              </span>
            </div>
            <div className="debug-row">
              <span className="debug-label">Messages (Conversation)</span>
              <span className="debug-value" data-testid="debug-messages-db">
                {threadInfo?.canonical_conversation?.message_count ?? '—'}
              </span>
            </div>
            <div className="debug-row">
              <span className="debug-label">Messages (UI)</span>
              <span className="debug-value" data-testid="debug-messages-ui">
                {state.messages.length}
              </span>
            </div>
            <div className="debug-row">
              <span className="debug-label">Messages (Scratch)</span>
              <span className="debug-value">
                {threadInfo?.message_count ?? '—'}
              </span>
            </div>
            <div className="debug-row">
              <span className="debug-label">Streaming</span>
              <span className="debug-value">{state.streamingContent ? 'Yes' : 'No'}</span>
            </div>
          </div>

          {/* Voice State (OpenAI realtime session, NOT backend WS) */}
          <div className="debug-section">
            <div className="debug-section-header">Voice (OpenAI)</div>
            <div className="debug-row">
              <span className="debug-label">Status</span>
              <span className="debug-value">
                <span
                  className="debug-indicator"
                  style={{ background: voiceStatusColor[state.voiceStatus] || 'var(--color-text-muted)' }}
                />
                {state.voiceStatus}
              </span>
            </div>
            <div className="debug-row">
              <span className="debug-label">Mode</span>
              <span className="debug-value">{state.voiceMode}</span>
            </div>
          </div>

          {/* Quick Links */}
          <div className="debug-section">
            <div className="debug-section-header">API</div>
            <a
              href={`${config.apiBaseUrl}/oikos/thread`}
              target="_blank"
              rel="noopener noreferrer"
              className="debug-link"
            >
              Thread →
            </a>
            <a
              href={`${config.apiBaseUrl}/oikos/history?limit=10&surface_id=web`}
              target="_blank"
              rel="noopener noreferrer"
              className="debug-link"
            >
              Web History →
            </a>
            <a
              href={`${config.apiBaseUrl}/oikos/history?limit=10&view=all`}
              target="_blank"
              rel="noopener noreferrer"
              className="debug-link"
            >
              All Activity →
            </a>
          </div>
        </div>
      </div>
    </>
  )
}
