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
import { DebugPanel, Header, ChatContainer, TextInput, OfflineBanner, ModelSelector } from './components'
import { supervisorToolStore } from '../lib/supervisor-tool-store'
import { eventBus } from '../lib/event-bus'
import config from '../../lib/config'

console.info('[Jarvis] Starting React application')

interface AppProps {
  embedded?: boolean
}

export default function App({ embedded = false }: AppProps) {
  const state = useAppState()
  const dispatch = useAppDispatch()
  const [isResetting, setIsResetting] = useState(false)

  // Show debug panel for developers (local dev mode)
  // In production, could also check user.role === 'ADMIN'
  const showDebugPanel = config.isDevelopment

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

  // Reset memory - clears supervisor thread history
  const handleReset = useCallback(async () => {
    console.log('[App] Resetting memory (clearing history)')
    setIsResetting(true)
    try {
      supervisorToolStore.clearTools()
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

  // Marketing ready signal - indicates chat is ready for screenshot capture
  useEffect(() => {
    if (state.messages.length > 0) {
      document.body.setAttribute('data-ready', 'true')
    }
    return () => document.body.removeAttribute('data-ready')
  }, [state.messages.length])

  // E2E test ready signal - emits when chat UI is interactive (DEV mode only)
  // This allows tests to wait for the chat to be ready instead of using arbitrary timeouts
  useEffect(() => {
    if (config.isDevelopment) {
      // Chat is ready when the text channel is initialized and not currently sending
      // This indicates the UI is mounted and interactive
      eventBus.emit('test:chat_ready', { timestamp: Date.now() })
    }
  }, []) // Empty deps = emit once on mount

  return (
    <>
      <OfflineBanner />
      <div className="app-container">
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
      </div>
    </>
  )
}
