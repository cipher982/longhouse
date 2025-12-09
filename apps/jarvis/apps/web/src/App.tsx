/**
 * Jarvis PWA - React App
 * Main application component using Context for state management
 */

import { useCallback } from 'react'
import { useAppState, useAppDispatch, type ChatMessage } from './context'
import { Sidebar, Header, VoiceControls, ChatContainer, TextInput } from './components'

export default function App() {
  const state = useAppState()
  const dispatch = useAppDispatch()

  // Sidebar handlers
  const handleToggleSidebar = useCallback(() => {
    dispatch({ type: 'SET_SIDEBAR_OPEN', open: !state.sidebarOpen })
  }, [dispatch, state.sidebarOpen])

  const handleNewConversation = useCallback(() => {
    console.log('New conversation')
    dispatch({ type: 'SET_MESSAGES', messages: [] })
    dispatch({ type: 'SET_CONVERSATION_ID', id: null })
    dispatch({ type: 'SET_STREAMING_CONTENT', content: '' })
  }, [dispatch])

  const handleClearAll = useCallback(() => {
    console.log('Clear all conversations')
    dispatch({ type: 'SET_CONVERSATIONS', conversations: [] })
    dispatch({ type: 'SET_MESSAGES', messages: [] })
  }, [dispatch])

  const handleSelectConversation = useCallback(
    (id: string) => {
      console.log('Select conversation:', id)
      dispatch({ type: 'SET_CONVERSATION_ID', id })
      // TODO: Load conversation messages from storage
    },
    [dispatch]
  )

  // Header handlers
  const handleSync = useCallback(() => {
    console.log('Sync conversations')
    // TODO: Implement sync with backend
  }, [])

  // Voice handlers
  const handleModeToggle = useCallback(() => {
    const newMode = state.voiceMode === 'push-to-talk' ? 'hands-free' : 'push-to-talk'
    dispatch({ type: 'SET_VOICE_MODE', mode: newMode })
  }, [dispatch, state.voiceMode])

  const handleVoiceButtonPress = useCallback(() => {
    console.log('Voice button pressed')
    dispatch({ type: 'SET_VOICE_STATUS', status: 'listening' })
    // TODO: Wire up real voice logic
  }, [dispatch])

  const handleVoiceButtonRelease = useCallback(() => {
    console.log('Voice button released')
    dispatch({ type: 'SET_VOICE_STATUS', status: 'idle' })
    // TODO: Wire up real voice logic
  }, [dispatch])

  // Text input handler
  const handleSendMessage = useCallback(
    (text: string) => {
      const newMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'user',
        content: text,
        timestamp: new Date(),
      }
      dispatch({ type: 'ADD_MESSAGE', message: newMessage })
      console.log('Send message:', text)
      // TODO: Send to backend and handle response
    },
    [dispatch]
  )

  // Map voice status for VoiceControls component
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
          messages={state.messages}
          isStreaming={state.streamingContent.length > 0}
          streamingContent={state.streamingContent}
        />

        <VoiceControls
          mode={state.voiceMode}
          status={voiceStatusMap[state.voiceStatus] || 'ready'}
          onModeToggle={handleModeToggle}
          onVoiceButtonPress={handleVoiceButtonPress}
          onVoiceButtonRelease={handleVoiceButtonRelease}
        />

        <TextInput onSend={handleSendMessage} />
      </div>

      {/* Supervisor Progress Panel (hidden by default) */}
      <div id="supervisor-progress" className="supervisor-progress-panel hidden"></div>

      {/* Hidden audio element for remote playback */}
      <audio id="remoteAudio" autoPlay style={{ display: 'none' }}></audio>
    </div>
  )
}
