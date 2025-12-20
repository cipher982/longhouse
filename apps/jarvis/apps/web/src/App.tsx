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
import { Sidebar, Header, VoiceControls, ChatContainer, TextInput, OfflineBanner, ModelSelector } from './components'
import { supervisorProgress } from '../lib/supervisor-progress'

console.info('[Jarvis] Starting React application')

export default function App() {
  const state = useAppState()
  const dispatch = useAppDispatch()

  // Initialize supervisor progress UI (sticky, stays at top of chat area)
  useEffect(() => {
    supervisorProgress.initialize('supervisor-progress', 'sticky')
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

  // Voice handlers
  const handleModeToggle = useCallback(() => {
    jarvisApp.toggleHandsFree()
  }, [jarvisApp])

  const handleVoiceButtonPress = useCallback(() => {
    jarvisApp.handlePTTPress()
  }, [jarvisApp])

  const handleVoiceButtonRelease = useCallback(() => {
    jarvisApp.handlePTTRelease()
  }, [jarvisApp])

  // Map voice status for component
  const voiceStatusMap: Record<string, 'idle' | 'connecting' | 'ready' | 'listening' | 'processing' | 'speaking' | 'error'> = {
    idle: 'idle',
    connecting: 'connecting',
    ready: 'ready',
    listening: 'listening',
    processing: 'processing',
    speaking: 'speaking',
    error: 'error',
  }

  // Handle connect request from VoiceControls
  const handleConnect = useCallback(() => {
    jarvisApp.reconnect()
  }, [jarvisApp])

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
        <Header onSync={handleSync} />

        <ChatContainer
          messages={state.messages}
          userTranscriptPreview={state.userTranscriptPreview}
        />

        <div className="bottom-controls">
          <VoiceControls
            mode={state.voiceMode}
            status={voiceStatusMap[state.voiceStatus] || 'idle'}
            onModeToggle={handleModeToggle}
            onVoiceButtonPress={handleVoiceButtonPress}
            onVoiceButtonRelease={handleVoiceButtonRelease}
            onConnect={handleConnect}
          />

          <div className="input-controls">
            <ModelSelector />
            <TextInput
              onSend={textChannel.sendMessage}
              disabled={textChannel.isSending}
            />
          </div>
        </div>
      </div>

      {/* Supervisor progress container (SupervisorProgressUI will normalize/relocate as needed) */}
      <div id="supervisor-progress"></div>

      {/* Hidden audio element for remote playback */}
      <audio id="remoteAudio" autoPlay style={{ display: 'none' }}></audio>
      </div>
    </>
  )
}
