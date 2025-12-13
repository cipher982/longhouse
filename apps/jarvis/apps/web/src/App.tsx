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

console.info('[Jarvis] Starting React application with realtime session integration')

export default function App() {
  const state = useAppState()
  const dispatch = useAppDispatch()

  // Initialize supervisor progress UI (floating, always visible)
  useEffect(() => {
    supervisorProgress.initialize('supervisor-progress', 'floating')
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
    const sessionManager = stateManager.getState().sessionManager
    if (!sessionManager) {
      console.warn('[App] Cannot create new conversation - sessionManager not ready')
      return
    }

    // 1. Create new conversation in persistence (SSOT)
    const newId = await sessionManager.createNewConversation()
    console.log('[App] New conversation created:', newId)

    // Update sidebar conversation list
    try {
      const allConversations = await sessionManager.getAllConversations()
      dispatch({ type: 'SET_CONVERSATIONS', conversations: toSidebarConversations(allConversations, newId) })
    } catch (error) {
      console.warn('[App] Failed to refresh conversation list:', error)
    }

    // 2. Update all controllers with the new ID
    conversationController.setConversationId(newId)
    stateManager.setConversationId(newId)

    // 3. Clear UI and set the NEW ID (not null!)
    textChannel.clearMessages()
    dispatch({ type: 'SET_MESSAGES', messages: [] })
    dispatch({ type: 'SET_CONVERSATION_ID', id: newId })

    // 4. Reconnect session with empty history for fresh context
    console.log('[App] Reconnecting session for new conversation context...')
    try {
      await realtimeSession.reconnect()
      console.log('[App] Session reconnected with fresh context')
    } catch (error) {
      console.warn('[App] Reconnect failed:', error)
    }
  }, [dispatch, textChannel, realtimeSession])

  const handleClearAll = useCallback(async () => {
    const sessionManager = stateManager.getState().sessionManager
    if (!sessionManager) {
      console.warn('[App] Clear all conversations - sessionManager not ready, skipping')
      return
    }
    console.log('[App] Clear all conversations - starting...')
    try {
      await sessionManager.clearAllConversations()
      console.log('[App] Clear all conversations - IndexedDB cleared successfully')
    } catch (error) {
      console.error('[App] Clear all conversations - failed:', error)
      return
    }
    textChannel.clearMessages()
    dispatch({ type: 'SET_MESSAGES', messages: [] })
    dispatch({ type: 'SET_CONVERSATIONS', conversations: [] })
    dispatch({ type: 'SET_CONVERSATION_ID', id: null })
    stateManager.setConversationId(null)
    conversationController.setConversationId(null)

    // Only reset Realtime context if the user is already connected.
    if (realtimeSession.isConnected()) {
      console.log('[App] Clear all conversations - realtime connected; reconnecting to clear model context...')
      try {
        await realtimeSession.reconnect()
        console.log('[App] Clear all conversations - session reconnected with fresh context')
      } catch (error) {
        console.warn('[App] Clear all conversations - reconnect failed:', error)
      }
    } else {
      console.log('[App] Clear all conversations - realtime not connected; skipping reconnect')
    }
    console.log('[App] Clear all conversations - complete')
  }, [dispatch, textChannel, realtimeSession])

  const handleSelectConversation = useCallback(
    async (id: string) => {
      console.log('[App] Switching to conversation:', id)
      const sessionManager = stateManager.getState().sessionManager
      if (!sessionManager) {
        console.warn('[App] Cannot switch conversation - sessionManager not ready')
        return
      }

      // 1. Switch persistence layer (SSOT - do this first!)
      await sessionManager.switchToConversation(id)
      conversationController.setConversationId(id)
      stateManager.setConversationId(id)

      // Update sidebar active state + metadata
      try {
        const allConversations = await sessionManager.getAllConversations()
        dispatch({ type: 'SET_CONVERSATIONS', conversations: toSidebarConversations(allConversations, id) })
      } catch (error) {
        console.warn('[App] Failed to refresh conversation list:', error)
      }

      // 2. Update React state with conversation ID
      dispatch({ type: 'SET_CONVERSATION_ID', id })

      // 3. Clear current messages while we reconnect
      // History will be loaded via SSOT callback during reconnect
      dispatch({ type: 'SET_MESSAGES', messages: [] })

      // 4. Reconnect session - this triggers bootstrapSession() which:
      //    - Loads history ONCE from IndexedDB (SSOT)
      //    - Hydrates Realtime with history
      //    - Calls onHistoryLoaded callback to update UI
      console.log('[App] Reconnecting session for new conversation context...')
      try {
        await realtimeSession.reconnect()
        console.log('[App] Session reconnected with conversation history (SSOT)')
      } catch (error) {
        console.warn('[App] Reconnect failed:', error)
      }
    },
    [dispatch, realtimeSession]
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
          messages={textChannel.messages}
          isStreaming={textChannel.isStreaming}
          streamingContent={textChannel.streamingContent}
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
