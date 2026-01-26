/**
 * DebugPanel - Dev/Admin sidebar with high-signal debug info
 * Only visible to developers (isDevelopment) or admins (user.role === 'ADMIN')
 */

import { useState, useEffect, useCallback } from 'react'
import { useAppState } from '../context'
import config from '../../../lib/config'
import './DebugPanel.css'

interface ThreadInfo {
  thread_id: number
  title: string
  message_count: number
}

interface DebugPanelProps {
  isOpen: boolean
  onToggle: () => void
  onReset: () => void
  isResetting?: boolean
}

export function DebugPanel({ isOpen, onToggle, onReset, isResetting = false }: DebugPanelProps) {
  const state = useAppState()
  const [threadInfo, setThreadInfo] = useState<ThreadInfo | null>(null)

  // Fetch thread info
  const fetchThreadInfo = useCallback(async () => {
    try {
      const response = await fetch(`${config.apiBaseUrl}/jarvis/concierge/thread`, {
        credentials: 'include',
      })
      if (response.ok) {
        const data = await response.json()
        setThreadInfo(data)
      }
    } catch (error) {
      console.warn('[DebugPanel] Failed to fetch thread info:', error)
    }
  }, [])

  useEffect(() => {
    fetchThreadInfo()
    // Always poll every 10 seconds - panel is always visible on desktop
    // (isOpen only controls mobile slide-in animation)
    const interval = setInterval(fetchThreadInfo, 10000)
    return () => clearInterval(interval)
  }, [fetchThreadInfo])

  // Refresh after messages change
  useEffect(() => {
    fetchThreadInfo()
  }, [state.messages.length, fetchThreadInfo])

  const handleReset = async () => {
    await onReset()
    // Refresh thread info after reset completes
    await fetchThreadInfo()
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
              <span className="debug-label">Messages (DB)</span>
              <span className="debug-value" data-testid="debug-messages-db">
                {threadInfo?.message_count ?? '—'}
              </span>
            </div>
            <div className="debug-row">
              <span className="debug-label">Messages (UI)</span>
              <span className="debug-value" data-testid="debug-messages-ui">
                {state.messages.length}
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
              href={`${config.apiBaseUrl}/jarvis/concierge/thread`}
              target="_blank"
              rel="noopener noreferrer"
              className="debug-link"
            >
              Thread →
            </a>
            <a
              href={`${config.apiBaseUrl}/jarvis/history?limit=10`}
              target="_blank"
              rel="noopener noreferrer"
              className="debug-link"
            >
              History →
            </a>
          </div>
        </div>
      </div>
    </>
  )
}
