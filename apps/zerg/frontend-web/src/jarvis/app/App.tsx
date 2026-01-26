/**
 * Jarvis PWA - React App
 * Main application component with realtime session integration
 *
 * This is a pure React application. useJarvisApp manages initialization,
 * connection, and voice state. useTextChannel handles text messaging.
 */

import { useCallback, useEffect, useState } from 'react'
import { useAppState, useAppDispatch } from './context'
import { useTextChannel } from './hooks'
import { useJarvisApp } from './hooks/useJarvisApp'
import { DebugPanel, Header, ChatContainer, TextInput, OfflineBanner, ModelSelector, CourseStatusIndicator, TraceIdDisplay } from './components'
import './components/TraceIdDisplay.css'
import { conciergeToolStore } from '../lib/concierge-tool-store'
import { eventBus } from '../lib/event-bus'
import config from '../../lib/config'
import { useAuth } from '../../lib/auth'

console.info('[Jarvis] Starting React application')

interface AppProps {
  embedded?: boolean
}

export default function App({ embedded = false }: AppProps) {
  const state = useAppState()
  const dispatch = useAppDispatch()
  const [isResetting, setIsResetting] = useState(false)
  const { user } = useAuth()

  // Show debug panel for developers (local dev mode) or admins in production
  const isAdmin = user?.role === 'ADMIN'
  const showDebugPanel = config.isDevelopment || isAdmin

  // Pause expensive CSS animations when window loses focus (saves CPU/GPU)
  useEffect(() => {
    const container = document.querySelector('.jarvis-container')
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

  // Main Jarvis app hook - handles initialization, connection, voice
  const jarvisApp = useJarvisApp({
    autoConnect: false, // User must click Connect button
    onConnected: () => console.log('[App] Connected'),
    onDisconnected: () => console.log('[App] Disconnected'),
    onTranscript: (text, isFinal) => {
      console.log('[App] Transcript:', text, isFinal ? '(final)' : '(partial)')
    },
    onError: (error) => console.error('[App] Error:', error),
  })

  // Text channel handling (always active)
  const textChannel = useTextChannel({
    onMessageSent: (msg) => console.log('[App] Message sent:', msg.content),
    onResponse: (msg) => console.log('[App] Response received:', msg.content),
    onError: (error) => console.error('[App] Text channel error:', error),
  })

  // Debug panel toggle
  const handleToggleDebugPanel = useCallback(() => {
    dispatch({ type: 'SET_SIDEBAR_OPEN', open: !state.sidebarOpen })
  }, [dispatch, state.sidebarOpen])

  // Reset memory - clears concierge thread history
  const handleReset = useCallback(async () => {
    console.log('[App] Resetting memory (clearing history)')
    setIsResetting(true)
    try {
      conciergeToolStore.clearTools()
      await jarvisApp.clearHistory()
      console.log('[App] Memory reset complete')
    } catch (error) {
      console.warn('[App] Reset failed:', error)
    } finally {
      setIsResetting(false)
    }
  }, [jarvisApp])

  // Header handlers
  const handleSync = useCallback(() => {
    console.log('[App] Sync conversations')
  }, [])

  // Voice handlers for mic button
  const handleMicConnect = useCallback(() => {
    jarvisApp.reconnect()
  }, [jarvisApp])

  const handleMicPressStart = useCallback(() => {
    jarvisApp.handlePTTPress()
  }, [jarvisApp])

  const handleMicPressEnd = useCallback(() => {
    jarvisApp.handlePTTRelease()
  }, [jarvisApp])

  // Map voice status for mic button
  const micStatus = state.voiceStatus as 'idle' | 'connecting' | 'ready' | 'listening' | 'processing' | 'speaking' | 'error'

  // Readiness Contract (see src/lib/readiness-contract.ts):
  // - data-ready="true": Page is INTERACTIVE (can click, type)
  // - data-screenshot-ready="true": Content loaded for marketing captures
  //
  // Chat uses window.__jarvis.ready.chatReady as the authoritative interactive signal.
  // We sync data-ready to this flag to match dashboard/canvas behavior.
  useEffect(() => {
    // Set up the chatReady flag and data-ready attribute together
    // This ensures consistent behavior with dashboard/canvas pages
    type JarvisWindow = Window & { __jarvis?: { ready?: { chatReady?: boolean; chatReadyTimestamp?: number }; eventBus?: unknown } }
    const w = window as JarvisWindow
    w.__jarvis = w.__jarvis || {}
    w.__jarvis.ready = w.__jarvis.ready || {}
    w.__jarvis.ready.chatReady = true
    w.__jarvis.ready.chatReadyTimestamp = Date.now()

    // Set data-ready when chat is interactive (matches dashboard/canvas contract)
    document.body.setAttribute('data-ready', 'true')

    // Emit event for backwards compatibility (DEV mode only)
    if (config.isDevelopment) {
      eventBus.emit('test:chat_ready', { timestamp: Date.now() })
    }

    return () => {
      document.body.removeAttribute('data-ready')
      const w2 = window as JarvisWindow
      if (w2.__jarvis?.ready) {
        w2.__jarvis.ready.chatReady = false
      }
    }
  }, []) // Empty deps = set once on mount, clear on unmount

  // Screenshot ready signal - indicates content is loaded for marketing captures
  // This is separate from data-ready because screenshots need visible content,
  // while interactive readiness just needs the UI to be mounted and responsive.
  useEffect(() => {
    if (state.messages.length > 0) {
      document.body.setAttribute('data-screenshot-ready', 'true')
    }
    return () => document.body.removeAttribute('data-screenshot-ready')
  }, [state.messages.length])

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
            onSync={handleSync}
            onReset={handleReset}
            isResetting={isResetting}
          />
        )}

        <div className="chat-settings-bar">
          <ModelSelector />
        </div>

        <ChatContainer
          messages={state.messages}
          userTranscriptPreview={state.userTranscriptPreview}
        />

        <div className="bottom-controls">
          <TextInput
            onSend={textChannel.sendMessage}
            disabled={textChannel.isSending}
            micStatus={micStatus}
            onMicConnect={handleMicConnect}
            onMicPressStart={handleMicPressStart}
            onMicPressEnd={handleMicPressEnd}
          />
        </div>
      </div>

      {/* Hidden audio element for remote playback */}
      <audio id="remoteAudio" autoPlay style={{ display: 'none' }}></audio>

      {/* Run status indicator for E2E testing - hidden but accessible via data-testid */}
      <CourseStatusIndicator />

      {/* Trace ID display for debugging - shows in dev mode only */}
      <TraceIdDisplay devOnly={true} />
    </>
  )
}
