/**
 * Jarvis PWA - React App
 * Main application component with realtime session integration
 *
 * This is a pure React application. Controllers emit events via stateManager,
 * and React hooks subscribe to those events and update React state.
 */

import { useCallback, useRef } from 'react'
import { useAppState, useAppDispatch, type ChatMessage } from './context'
import { useTextChannel, useRealtimeSession } from './hooks'
import { Sidebar, Header, VoiceControls, ChatContainer, TextInput, OfflineBanner } from './components'
import { conversationController } from '../lib/conversation-controller'
import { stateManager } from '../lib/state-manager'

console.info('[Jarvis] Starting React application with realtime session integration')

export default function App() {
  const state = useAppState()
  const dispatch = useAppDispatch()
  const historyLoadedRef = useRef(false)

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

  // Realtime session - always active
  // History is loaded via SSOT callback in useRealtimeSession during connect()
  const realtimeSession = useRealtimeSession({
    onConnected: () => console.log('[App] Realtime session connected'),
    onDisconnected: () => console.log('[App] Realtime session disconnected'),
    onTranscript: (text, isFinal) => {
      console.log('[App] Transcript:', text, isFinal ? '(final)' : '(partial)')
      if (isFinal && text.trim()) {
        // Add user message from voice transcript
        const userMessage: ChatMessage = {
          id: crypto.randomUUID(),
          role: 'user',
          content: text,
          timestamp: new Date(),
        }
        dispatch({ type: 'ADD_MESSAGE', message: userMessage })
      }
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

    // 2. Update all controllers with the new ID
    conversationController.setConversationId(newId)
    stateManager.setConversationId(newId)

    // 3. Clear UI and set the NEW ID (not null!)
    textChannel.clearMessages()
    dispatch({ type: 'SET_MESSAGES', messages: [] })
    dispatch({ type: 'SET_CONVERSATION_ID', id: newId })

    // 4. Reset history loaded flag so we can load new history on next conversation
    historyLoadedRef.current = false

    // 5. Reconnect session with empty history for fresh context
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

    // Reconnect session to clear the model's context (history was already injected)
    console.log('[App] Clear all conversations - reconnecting session to clear model context...')
    try {
      await realtimeSession.reconnect()
      console.log('[App] Clear all conversations - session reconnected with fresh context')
    } catch (error) {
      console.warn('[App] Clear all conversations - reconnect failed:', error)
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

      // 2. Update React state with conversation ID
      dispatch({ type: 'SET_CONVERSATION_ID', id })

      // 3. Load this conversation's history from IndexedDB
      const history = await sessionManager.getConversationHistory()
      console.log('[App] Loaded conversation history:', history.length, 'turns')

      // 4. Convert turns to messages and update UI
      const messages: ChatMessage[] = []
      for (const turn of history) {
        if (turn.userTranscript) {
          messages.push({
            id: turn.id || crypto.randomUUID(),
            role: 'user',
            content: turn.userTranscript,
            timestamp: turn.timestamp ? new Date(turn.timestamp) : new Date(),
          })
        }
        if (turn.assistantResponse) {
          messages.push({
            id: `${turn.id}-asst` || crypto.randomUUID(),
            role: 'assistant',
            content: turn.assistantResponse,
            timestamp: turn.timestamp ? new Date(turn.timestamp) : new Date(),
          })
        }
      }
      dispatch({ type: 'SET_MESSAGES', messages })

      // 5. Reset history loaded flag
      historyLoadedRef.current = true

      // 6. Reconnect session to hydrate new history into model context
      console.log('[App] Reconnecting session for new conversation context...')
      try {
        await realtimeSession.reconnect()
        console.log('[App] Session reconnected with conversation history')
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

      {/* Supervisor Progress Panel (hidden by default) */}
      <div id="supervisor-progress" className="supervisor-progress-panel hidden"></div>

      {/* Hidden audio element for remote playback */}
      <audio id="remoteAudio" autoPlay style={{ display: 'none' }}></audio>
      </div>
    </>
  )
}
