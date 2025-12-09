/**
 * Jarvis PWA - React App
 * Main application component with realtime session integration
 */

import { useCallback, useEffect, useState } from 'react'
import { useAppState, useAppDispatch, type ChatMessage } from './context'
import { useTextChannel, useRealtimeSession } from './hooks'
import { Sidebar, Header, VoiceControls, ChatContainer, TextInput, OfflineBanner } from './components'

// Feature flag for enabling realtime session bridge
// Controlled via VITE_JARVIS_ENABLE_REALTIME_BRIDGE env var
// Default: false (standalone React mode with simulated responses)
// Bridge mode: true (full functionality via legacy controllers - recommended for production)
const ENABLE_REALTIME_BRIDGE = import.meta.env.VITE_JARVIS_ENABLE_REALTIME_BRIDGE === 'true'

// Log info about active mode
if (ENABLE_REALTIME_BRIDGE) {
  console.info(
    '[Jarvis] Bridge mode active - using legacy controllers for full realtime functionality. ' +
    'This is the recommended mode for production until React hooks achieve feature parity.'
  )
} else {
  console.info(
    '[Jarvis] Standalone React mode active - responses are simulated. ' +
    'Set VITE_JARVIS_ENABLE_REALTIME_BRIDGE=true for full backend integration.'
  )
}

export default function App() {
  const state = useAppState()
  const dispatch = useAppDispatch()
  const [isInitialized, setIsInitialized] = useState(false)

  // Text channel handling (always active)
  const textChannel = useTextChannel({
    onMessageSent: (msg) => console.log('[App] Message sent:', msg.content),
    onResponse: (msg) => console.log('[App] Response received:', msg.content),
    onError: (error) => console.error('[App] Text channel error:', error),
  })

  // Realtime session bridge (optional, controlled by feature flag)
  const realtimeSession = ENABLE_REALTIME_BRIDGE
    ? useRealtimeSession({
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
    : null

  // Initialize standalone mode if not using realtime bridge
  useEffect(() => {
    if (!ENABLE_REALTIME_BRIDGE) {
      setIsInitialized(true)
    }
  }, [])

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
    if (realtimeSession) {
      realtimeSession.toggleHandsFree()
    } else {
      const newMode = state.voiceMode === 'push-to-talk' ? 'hands-free' : 'push-to-talk'
      dispatch({ type: 'SET_VOICE_MODE', mode: newMode })
    }
  }, [dispatch, state.voiceMode, realtimeSession])

  const handleVoiceButtonPress = useCallback(() => {
    if (realtimeSession) {
      realtimeSession.handlePTTPress()
    } else {
      console.log('[App] Voice button pressed (standalone mode)')
      dispatch({ type: 'SET_VOICE_STATUS', status: 'listening' })
    }
  }, [dispatch, realtimeSession])

  const handleVoiceButtonRelease = useCallback(() => {
    if (realtimeSession) {
      realtimeSession.handlePTTRelease()
    } else {
      console.log('[App] Voice button released (standalone mode)')
      dispatch({ type: 'SET_VOICE_STATUS', status: 'idle' })
    }
  }, [dispatch, realtimeSession])

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
    if (realtimeSession) {
      realtimeSession.reconnect()
    } else {
      console.log('[App] Connect requested (standalone mode)')
      dispatch({ type: 'SET_VOICE_STATUS', status: 'ready' })
    }
  }, [dispatch, realtimeSession])

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
