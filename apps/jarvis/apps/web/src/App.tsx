/**
 * Jarvis PWA - React App
 * Main application component using hooks for business logic
 */

import { useCallback, useEffect } from 'react'
import { useAppState, useAppDispatch } from './context'
import { useVoice, useTextChannel, useJarvisClient } from './hooks'
import { Sidebar, Header, VoiceControls, ChatContainer, TextInput } from './components'

export default function App() {
  const state = useAppState()
  const dispatch = useAppDispatch()

  // Initialize Jarvis client
  const { initialize: initClient } = useJarvisClient({
    autoConnect: true,
    onConnected: () => console.log('[App] Connected to Zerg backend'),
    onError: (error) => console.error('[App] Client error:', error),
  })

  // Voice handling
  const voice = useVoice({
    onTranscript: (text, isFinal) => {
      console.log('[App] Transcript:', text, isFinal ? '(final)' : '(partial)')
    },
    onError: (error) => console.error('[App] Voice error:', error),
  })

  // Text channel handling
  const textChannel = useTextChannel({
    onMessageSent: (msg) => console.log('[App] Message sent:', msg.content),
    onResponse: (msg) => console.log('[App] Response received:', msg.content),
    onError: (error) => console.error('[App] Text channel error:', error),
  })

  // Initialize on mount
  useEffect(() => {
    initClient()
  }, [initClient])

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
      // TODO: Load conversation messages from storage
    },
    [dispatch]
  )

  // Header handlers
  const handleSync = useCallback(() => {
    console.log('[App] Sync conversations')
    // TODO: Implement sync with backend
  }, [])

  // Map voice status for component
  const voiceStatusMap: Record<string, 'ready' | 'listening' | 'processing' | 'speaking' | 'error'> = {
    idle: 'ready',
    connecting: 'processing',
    listening: 'listening',
    processing: 'processing',
    speaking: 'speaking',
    error: 'error',
  }

  return (
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
          mode={voice.mode}
          status={voiceStatusMap[state.voiceStatus] || 'ready'}
          onModeToggle={voice.toggleMode}
          onVoiceButtonPress={voice.handlePTTPress}
          onVoiceButtonRelease={voice.handlePTTRelease}
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
  )
}
