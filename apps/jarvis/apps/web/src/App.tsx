/**
 * Jarvis PWA - React App
 * Main application component
 */

export default function App() {
  return (
    <div className="app-container">
      {/* Sidebar Toggle (mobile) */}
      <button
        className="sidebar-toggle"
        id="sidebarToggle"
        type="button"
        aria-label="Open conversation sidebar"
        aria-expanded="false"
      >
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M3 12h18M3 6h18M3 18h18" />
        </svg>
      </button>

      {/* Sidebar */}
      <div className="sidebar" id="sidebar">
        <div className="sidebar-header">
          <h2>Conversations</h2>
        </div>
        <div className="sidebar-content">
          <button id="newConversationBtn" className="sidebar-button primary">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 5v14M5 12h14" />
            </svg>
            New Conversation
          </button>
          <button id="clearConvosBtn" className="sidebar-button danger">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14zM10 11v6M14 11v6" />
            </svg>
            Clear All
          </button>
          <div id="conversationList">
            <div className="conversation-item empty">
              <div className="conversation-name">No conversations yet</div>
              <div className="conversation-meta">Start your first conversation</div>
            </div>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="main-content">
        <div className="main-header">
          <h1 id="appTitle">Jarvis AI</h1>
          <div className="header-actions">
            <a href="/dashboard" className="header-button" title="Open Dashboard">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="3" y="3" width="7" height="7" />
                <rect x="14" y="3" width="7" height="7" />
                <rect x="14" y="14" width="7" height="7" />
                <rect x="3" y="14" width="7" height="7" />
              </svg>
              <span>Dashboard</span>
            </a>
            <button id="syncNowBtn" className="header-button" title="Sync conversations">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M23 4v6h-6M1 20v-6h6M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.64A9 9 0 0 1 3.51 15" />
              </svg>
              <span>Sync</span>
            </button>
          </div>
        </div>

        <div className="chat-container">
          <div id="transcript">
            {/* Chat messages will render here */}
            <div className="status-message">
              React migration in progress...
            </div>
          </div>
        </div>

        {/* Voice Controls */}
        <div className="voice-controls">
          <div className="mode-toggle-container">
            <span className="mode-label">Push-to-Talk</span>
            <button
              id="handsFreeToggle"
              className="mode-toggle"
              type="button"
              role="switch"
              aria-checked="false"
              aria-label="Toggle hands-free mode"
            >
              <span className="mode-toggle-slider"></span>
            </button>
            <span className="mode-label">Hands-free</span>
          </div>

          <div className="voice-button-wrapper">
            <button id="pttBtn" className="voice-button" type="button" aria-label="Press to talk">
              <div className="voice-button-ring"></div>
              <svg className="voice-icon" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z" />
                <path d="M19 10v2a7 7 0 01-14 0v-2" />
                <line x1="12" y1="19" x2="12" y2="23" />
                <line x1="8" y1="23" x2="16" y2="23" />
              </svg>
            </button>
          </div>

          <div className="voice-status">
            <span className="voice-status-text">Ready</span>
          </div>
        </div>

        {/* Text Input */}
        <div className="text-input-container">
          <input
            type="text"
            id="textInput"
            className="text-input"
            placeholder="Type a message..."
            aria-label="Message input"
          />
          <button id="sendTextBtn" className="send-button" type="button" aria-label="Send message">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="22" y1="2" x2="11" y2="13" />
              <polygon points="22 2 15 22 11 13 2 9 22 2" />
            </svg>
          </button>
        </div>
      </div>

      {/* Supervisor Progress Panel */}
      <div id="supervisor-progress" className="supervisor-progress-panel hidden"></div>

      {/* Hidden audio element for remote playback */}
      <audio id="remoteAudio" autoPlay style={{ display: 'none' }}></audio>
    </div>
  )
}
