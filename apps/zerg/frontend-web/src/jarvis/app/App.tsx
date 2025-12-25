/**
 * Jarvis PWA - React App
 * Main application component with realtime session integration
 *
 * This is a pure React application. useJarvisApp manages initialization,
 * connection, and voice state. useTextChannel handles text messaging.
 */

import { useCallback, useEffect } from 'react'
import { useAppState, useAppDispatch } from './context'
import { useTextChannel } from './hooks'
import { useJarvisApp } from './hooks/useJarvisApp'
import { Sidebar, Header, ChatContainer, TextInput, OfflineBanner, ModelSelector, WorkerProgress } from './components'

console.info('[Jarvis] Starting React application')

interface AppProps {
  embedded?: boolean
}

export default function App({ embedded = false }: AppProps) {
  const state = useAppState()
  const dispatch = useAppDispatch()

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

  // Sidebar handlers
  const handleToggleSidebar = useCallback(() => {
    dispatch({ type: 'SET_SIDEBAR_OPEN', open: !state.sidebarOpen })
  }, [dispatch, state.sidebarOpen])

  const handleNewConversation = useCallback(async () => {
    console.log('[App] Creating new conversation')
    dispatch({ type: 'SET_MESSAGES', messages: [] })
    dispatch({ type: 'SET_CONVERSATION_ID', id: null })
    console.log('[App] New conversation ready')
  }, [dispatch])

  const handleClearAll = useCallback(async () => {
    console.log('[App] Clear all conversations - starting...')
    try {
      await jarvisApp.clearHistory()
      console.log('[App] Clear all conversations - complete')
    } catch (error) {
      console.warn('[App] Clear all conversations - failed:', error)
    }
  }, [jarvisApp])

  const handleSelectConversation = useCallback(
    async (id: string) => {
      console.log('[App] Switching to conversation:', id)
      dispatch({ type: 'SET_CONVERSATION_ID', id })
      dispatch({ type: 'SET_MESSAGES', messages: [] })
      console.log('[App] Switched to conversation:', id)
    },
    [dispatch]
  )

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

  return (
    <>
      <OfflineBanner />
      <div className="app-container">
        <Sidebar
        conversations={state.conversations}
        isOpen={state.sidebarOpen}
        onToggle={handleToggleSidebar}
        onNewConversation={handleNewConversation}
        onClearAll={handleClearAll}
        onSelectConversation={handleSelectConversation}
      />

      <div className="main-content">
        {!embedded && <Header onSync={handleSync} />}

        <div className="chat-settings-bar">
          <ModelSelector />
        </div>

        <WorkerProgress mode="sticky" />

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
