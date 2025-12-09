/**
 * Jarvis PWA - React App
 * Main application component with realtime session integration
 *
 * This is a pure React application. Controllers emit events via stateManager,
 * and React hooks subscribe to those events and update React state.
 */

import { useCallback, useEffect, useState, useRef } from 'react'
import { useAppState, useAppDispatch, type ChatMessage } from './context'
import { useTextChannel, useRealtimeSession } from './hooks'
import { Sidebar, Header, VoiceControls, ChatContainer, TextInput, OfflineBanner } from './components'
import { conversationController } from '../lib/conversation-controller'

console.info('[Jarvis] Starting React application with realtime session integration')

export default function App() {
  const state = useAppState()
  const dispatch = useAppDispatch()
  const [isInitialized, setIsInitialized] = useState(false)
  const historyLoadedRef = useRef(false)

  // Load conversation history when session is ready
  // Only seeds messages if none exist yet (avoids overwriting live messages)
  useEffect(() => {
    if (!isInitialized || historyLoadedRef.current) return

    const loadHistory = async () => {
      try {
        const history = await conversationController.getHistory()
        // Only seed if no messages have been added yet (prevents race condition)
        if (history.length > 0 && state.messages.length === 0) {
          console.log('[App] Loading conversation history:', history.length, 'turns')
          const messages: ChatMessage[] = history.map((turn) => ({
            id: turn.id || crypto.randomUUID(),
            role: turn.userTranscript ? 'user' : 'assistant',
            content: turn.userTranscript || turn.assistantResponse || '',
            timestamp: turn.timestamp ? new Date(turn.timestamp) : new Date(),
          }))
          dispatch({ type: 'SET_MESSAGES', messages })
        }
      } catch (error) {
        console.error('[App] Failed to load conversation history:', error)
      } finally {
        // Always mark as loaded to prevent retry spam
        historyLoadedRef.current = true
      }
    }

    loadHistory()
  }, [isInitialized, dispatch, state.messages.length])

  // Text channel handling (always active)
  const textChannel = useTextChannel({
    onMessageSent: (msg) => console.log('[App] Message sent:', msg.content),
    onResponse: (msg) => console.log('[App] Response received:', msg.content),
    onError: (error) => console.error('[App] Text channel error:', error),
  })

  // Realtime session - always active
  const realtimeSession = useRealtimeSession({
    onConnected: () => {
      console.log('[App] Realtime session connected')
      setIsInitialized(true)
    },
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

  const handleNewConversation = useCallback(() => {
    console.log('[App] New conversation')
    textChannel.clearMessages()
    dispatch({ type: 'SET_CONVERSATION_ID', id: null })
  }, [dispatch, textChannel])

  const handleClearAll = useCallback(() => {
    console.log('[App] Clear all conversations')
    dispatch({ type: 'SET_CONVERSATIONS', conversations: [] })
    textChannel.clearMessages()
  }, [dispatch, textChannel])

  const handleSelectConversation = useCallback(
    (id: string) => {
      console.log('[App] Select conversation:', id)
      dispatch({ type: 'SET_CONVERSATION_ID', id })
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
