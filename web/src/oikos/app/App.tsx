/**
 * Oikos PWA - React App
 * Main application component with realtime session integration
 *
 * This is a pure React application. useOikosApp manages initialization,
 * connection, voice state, and the live chat controller. useTextChannel
 * handles optimistic text-input state.
 */

import { useCallback, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useAppState, useAppDispatch } from './context'
import { useTextChannel, useTurnBasedVoice } from './hooks'
import { useOikosApp } from './hooks/useOikosApp'
import { DebugPanel, Header, ChatContainer, TextInput, OfflineBanner, ModelSelector, QuotaPanel, RunStatusIndicator, TraceIdDisplay } from './components'
import './components/TraceIdDisplay.css'
import { oikosToolStore } from '../lib/oikos-tool-store'
import { eventBus } from '../lib/event-bus'
import config from '../../lib/config'
import { useAuth } from '../../lib/auth'
import { useReadinessFlag } from '../../lib/readiness-contract'
import { getQuotaUiState } from './lib/quota-ui'
import { fetchUserUsage } from './lib/usage'

console.info('[Oikos] Starting React application')

interface AppProps {
  embedded?: boolean
}

export default function App({ embedded = false }: AppProps) {
  const state = useAppState()
  const dispatch = useAppDispatch()
  const [isResetting, setIsResetting] = useState(false)
  const [isSwitchingHistoryView, setIsSwitchingHistoryView] = useState(false)
  const { user } = useAuth()

  // Show debug panel for developers (local dev mode) or admins in production
  const isAdmin = user?.role === 'ADMIN'
  const showDebugPanel = config.isDevelopment || isAdmin

  // Pause expensive CSS animations when window loses focus (saves CPU/GPU)
  useEffect(() => {
    const container = document.querySelector('.oikos-container')
    if (!container) return

    const handleVisibilityChange = () => {
      if (document.hidden) {
        container.classList.add('animations-paused')
      } else {
        container.classList.remove('animations-paused')
      }
    }

    document.addEventListener('visibilitychange', handleVisibilityChange)
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange)
  }, [])

  // Main Oikos app hook - handles initialization, connection, voice
  const oikosApp = useOikosApp()

  // Text channel handling (always active)
  const textChannel = useTextChannel({ sendText: oikosApp.sendText })

  const turnBasedVoice = useTurnBasedVoice({
    onError: (error) => console.error('[App] Voice error:', error),
    sendText: oikosApp.sendText,
  })

  const usageQuery = useQuery({
    queryKey: ['oikos-usage', 'today'],
    queryFn: () => fetchUserUsage('today'),
    refetchInterval: 30000,
    staleTime: 15000,
  })
  const quotaUi = getQuotaUiState(usageQuery.data)

  // Debug panel toggle
  const handleToggleDebugPanel = useCallback(() => {
    dispatch({ type: 'SET_SIDEBAR_OPEN', open: !state.sidebarOpen })
  }, [dispatch, state.sidebarOpen])

  // Reset memory - clears oikos thread history
  const handleReset = useCallback(async () => {
    console.log('[App] Resetting memory (clearing history)')
    setIsResetting(true)
    try {
      oikosToolStore.clearTools()
      await oikosApp.clearHistory()
      console.log('[App] Memory reset complete')
    } catch (error) {
      console.warn('[App] Reset failed:', error)
    } finally {
      setIsResetting(false)
    }
  }, [oikosApp])

  const handleToggleHistoryView = useCallback(async () => {
    const nextView = oikosApp.historyView === 'surface' ? 'all' : 'surface'
    setIsSwitchingHistoryView(true)
    try {
      await oikosApp.setHistoryView(nextView)
    } catch (error) {
      console.warn('[App] Failed to switch history view:', error)
    } finally {
      setIsSwitchingHistoryView(false)
    }
  }, [oikosApp])

  // Map voice status for mic button
  const micStatus = state.voiceStatus as 'idle' | 'connecting' | 'ready' | 'listening' | 'processing' | 'speaking' | 'error'

  // Readiness Contract (see src/lib/readiness-contract.ts):
  // - data-ready="true": Page is INTERACTIVE (can click, type)
  // - data-screenshot-ready="true": Content loaded for marketing captures
  //
  // Chat uses window.__oikos.ready.chatReady as the authoritative interactive signal.
  // The shared readiness hook mirrors the browser-facing flags.
  useReadinessFlag({
    ready: true,
    screenshotReady: state.messages.length > 0,
  })

  useEffect(() => {
    // Set up the chatReady flag for tests and external observers.
    type OikosWindow = Window & { __oikos?: { ready?: { chatReady?: boolean; chatReadyTimestamp?: number }; eventBus?: unknown } }
    const w = window as OikosWindow
    w.__oikos = w.__oikos || {}
    w.__oikos.ready = w.__oikos.ready || {}
    w.__oikos.ready.chatReady = true
    w.__oikos.ready.chatReadyTimestamp = Date.now()

    // Emit event for backwards compatibility (DEV mode only)
    if (config.isDevelopment) {
      eventBus.emit('test:chat_ready', { timestamp: Date.now() })
    }

    return () => {
      const w2 = window as OikosWindow
      if (w2.__oikos?.ready) {
        w2.__oikos.ready.chatReady = false
      }
    }
  }, []) // Empty deps = set once on mount, clear on unmount

  return (
    <>
      <OfflineBanner />

      {/* Debug panel - now renders as fixed overlay */}
      {showDebugPanel && (
        <DebugPanel
          isOpen={state.sidebarOpen}
          onToggle={handleToggleDebugPanel}
          onReset={handleReset}
          isResetting={isResetting}
        />
      )}

      <div className="main-content">
        {!embedded && (
          <Header
            onReset={handleReset}
            isResetting={isResetting}
          />
        )}

        <div className="chat-settings-bar">
          <div className="chat-settings-bar__left">
            <ModelSelector />
            <button
              type="button"
              className={`chat-surface-toggle${oikosApp.historyView === 'all' ? ' chat-surface-toggle--all' : ''}`}
              data-testid="surface-view-toggle"
              onClick={handleToggleHistoryView}
              disabled={isSwitchingHistoryView}
              title={oikosApp.historyView === 'all' ? 'Showing all activity across surfaces' : 'Showing web messages only'}
            >
              {isSwitchingHistoryView
                ? 'Switching...'
                : (oikosApp.historyView === 'all' ? 'All activity' : 'Web only')}
            </button>
          </div>
          <QuotaPanel
            usage={usageQuery.data}
            isLoading={usageQuery.isLoading}
            isError={usageQuery.isError}
          />
        </div>

        <ChatContainer
          messages={state.messages}
          userTranscriptPreview={state.userTranscriptPreview}
          showSurfaceBadges={oikosApp.historyView === 'all'}
        />

        <div className="bottom-controls">
          <TextInput
            onSend={textChannel.sendMessage}
            disabled={textChannel.isSending}
            inputDisabled={quotaUi.blocked}
            blockedReason={quotaUi.placeholderOverride}
            helperText={quotaUi.helperText}
            micStatus={micStatus}
            micLevel={turnBasedVoice.micLevel}
            onMicConnect={turnBasedVoice.resetVoice}
            onMicPressStart={turnBasedVoice.startRecording}
            onMicPressEnd={turnBasedVoice.stopRecording}
          />
        </div>
      </div>

      {/* Hidden audio element for remote playback */}
      <audio id="remoteAudio" autoPlay style={{ display: 'none' }}></audio>

      {/* Run status indicator for E2E testing - hidden but accessible via data-testid */}
      <RunStatusIndicator />

      {/* Trace ID display for debugging - shows in dev mode only */}
      <TraceIdDisplay devOnly={true} />
    </>
  )
}
