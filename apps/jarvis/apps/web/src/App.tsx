/**
 * Jarvis PWA - React App
 * Main application component
 */

import { useState, useCallback } from 'react'
import {
  Sidebar,
  Header,
  VoiceControls,
  ChatContainer,
  TextInput,
  type VoiceMode,
  type VoiceStatus,
  type ChatMessage,
} from './components'

export default function App() {
  // UI State
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [conversations, setConversations] = useState<{ id: string; name: string; meta: string; active?: boolean }[]>([])

  // Voice State
  const [voiceMode, setVoiceMode] = useState<VoiceMode>('push-to-talk')
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>('ready')

  // Chat State
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')

  // Sidebar handlers
  const handleToggleSidebar = useCallback(() => {
    setSidebarOpen((prev) => !prev)
  }, [])

  const handleNewConversation = useCallback(() => {
    console.log('New conversation')
    // TODO: Implement with real logic
  }, [])

  const handleClearAll = useCallback(() => {
    console.log('Clear all conversations')
    setConversations([])
    setMessages([])
  }, [])

  const handleSelectConversation = useCallback((id: string) => {
    console.log('Select conversation:', id)
    // TODO: Implement with real logic
  }, [])

  // Header handlers
  const handleSync = useCallback(() => {
    console.log('Sync conversations')
    // TODO: Implement with real logic
  }, [])

  // Voice handlers
  const handleModeToggle = useCallback(() => {
    setVoiceMode((prev) => (prev === 'push-to-talk' ? 'hands-free' : 'push-to-talk'))
  }, [])

  const handleVoiceButtonPress = useCallback(() => {
    console.log('Voice button pressed')
    setVoiceStatus('listening')
    // TODO: Implement with real voice logic
  }, [])

  const handleVoiceButtonRelease = useCallback(() => {
    console.log('Voice button released')
    setVoiceStatus('ready')
    // TODO: Implement with real voice logic
  }, [])

  // Text input handler
  const handleSendMessage = useCallback((text: string) => {
    const newMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: text,
      timestamp: new Date(),
    }
    setMessages((prev) => [...prev, newMessage])

    // TODO: Send to backend and handle response
    console.log('Send message:', text)
  }, [])

  return (
    <div className="app-container">
      <Sidebar
        conversations={conversations}
        isOpen={sidebarOpen}
        onToggle={handleToggleSidebar}
        onNewConversation={handleNewConversation}
        onClearAll={handleClearAll}
        onSelectConversation={handleSelectConversation}
      />

      <div className="main-content">
        <Header title="Jarvis AI" onSync={handleSync} />

        <ChatContainer
          messages={messages}
          isStreaming={isStreaming}
          streamingContent={streamingContent}
        />

        <VoiceControls
          mode={voiceMode}
          status={voiceStatus}
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
