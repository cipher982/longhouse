/**
 * Jarvis PWA - React App
 * Main application component with realtime session integration
 *
 * This is a pure React application. Controllers emit events via stateManager,
 * and React hooks subscribe to those events and update React state.
 */

import { useCallback, useEffect } from 'react'
import { useAppState, useAppDispatch } from './context'
import { useTextChannel, useRealtimeSession } from './hooks'
import { Sidebar, Header, VoiceControls, ChatContainer, TextInput, OfflineBanner } from './components'
import { conversationController } from '../lib/conversation-controller'
import { stateManager } from '../lib/state-manager'
import { toSidebarConversations } from '../lib/conversation-list'
import { supervisorProgress } from '../lib/supervisor-progress'
import { appController } from '../lib/app-controller'

console.info('[Jarvis] Starting React application with realtime session integration')

export default function App() {
  const state = useAppState()
  const dispatch = useAppDispatch()

  // Initialize supervisor progress UI (sticky, stays at top of chat area)
  useEffect(() => {
    supervisorProgress.initialize('supervisor-progress', 'sticky')
  }, [])

  // NOTE: History loading is now handled via SSOT in useRealtimeSession
  // appController.connect() calls bootstrapSession() which loads history ONCE
  // and provides it to BOTH the UI (via callback) and Realtime (via hydration)
  // This eliminates the two-query problem that caused UI/model divergence

  // Text channel handling (always active)
  const textChannel = useTextChannel({
    onMessageSent: (msg) => console.log('[App] Message sent:', msg.content),
    onResponse: (msg) => console.log('[App] Response received:', msg.content),
    onError: (error) => console.error('[App] Text channel error:', error),
  })

  // Realtime session - manual connect only
  // Auto-connect disabled to prevent:
  // 1. Scary "local network scanning" permission prompt on page load
  // 2. Mic permission request before user wants voice features
  // 3. Wasted API calls for visitors who just want text chat
  // User clicks mic button → manually triggers connection
  const realtimeSession = useRealtimeSession({
    autoConnect: false,  // User must click Connect button
    onConnected: () => console.log('[App] Realtime session connected'),
    onDisconnected: () => console.log('[App] Realtime session disconnected'),
    onTranscript: (text, isFinal) => {
      // Transcript events are for preview/logging only
      // User message is added via USER_VOICE_COMMITTED (placeholder) + USER_VOICE_TRANSCRIPT (content)
      console.log('[App] Transcript:', text, isFinal ? '(final)' : '(partial)')
    },
    onError: (error) => console.error('[App] Realtime error:', error),
  })

  // Sidebar handlers
  const handleToggleSidebar = useCallback(() => {
    dispatch({ type: 'SET_SIDEBAR_OPEN', open: !state.sidebarOpen })
  }, [dispatch, state.sidebarOpen])

  const handleNewConversation = useCallback(async () => {
    console.log('[App] Creating new conversation')

    // Clear UI state for new conversation
    dispatch({ type: 'SET_MESSAGES', messages: [] })
    dispatch({ type: 'SET_CONVERSATION_ID', id: null })

    console.log('[App] New conversation ready')
  }, [dispatch])

  const handleClearAll = useCallback(async () => {
    console.log('[App] Clear all conversations - starting...')

    // Clear server-side history (single source of truth)
    try {
      await appController.clearServerHistory()
      console.log('[App] Clear all conversations - server history cleared')
    } catch (error) {
      console.warn('[App] Clear all conversations - server clear failed:', error)
      return
    }

    // Clear local UI state
    dispatch({ type: 'SET_MESSAGES', messages: [] })

    console.log('[App] Clear all conversations - complete')
  }, [dispatch])

  const handleSelectConversation = useCallback(
    async (id: string) => {
      console.log('[App] Switching to conversation:', id)

      // For Supervisor backend, conversations are just different threads
      // We don't need to switch local state, just update the UI indicator
      dispatch({ type: 'SET_CONVERSATION_ID', id })

      // Clear current messages - history will be loaded from server
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
    realtimeSession.toggleHandsFree()
  }, [realtimeSession])

  const handleVoiceButtonPress = useCallback(() => {
    realtimeSession.handlePTTPress()
  }, [realtimeSession])

  const handleVoiceButtonRelease = useCallback(() => {
    realtimeSession.handlePTTRelease()
  }, [realtimeSession])

  // Map voice status for component - now uses full status including idle/connecting
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
    realtimeSession.reconnect()
  }, [realtimeSession])

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
        <Header title="Jarvis AI" onSync={handleSync} />

        <ChatContainer
          messages={state.messages}
          isStreaming={false}
          streamingContent=""
          userTranscriptPreview={state.userTranscriptPreview}
        />

        <VoiceControls
          mode={state.voiceMode}
          status={voiceStatusMap[state.voiceStatus] || 'idle'}
          onModeToggle={handleModeToggle}
          onVoiceButtonPress={handleVoiceButtonPress}
          onVoiceButtonRelease={handleVoiceButtonRelease}
          onConnect={handleConnect}
        />

        <TextInput
          onSend={textChannel.sendMessage}
          disabled={textChannel.isSending}
        />
      </div>

      {/* Supervisor progress container (SupervisorProgressUI will normalize/relocate as needed) */}
      <div id="supervisor-progress"></div>

      {/* Hidden audio element for remote playback */}
      <audio id="remoteAudio" autoPlay style={{ display: 'none' }}></audio>
      </div>
    </>
  )
}
