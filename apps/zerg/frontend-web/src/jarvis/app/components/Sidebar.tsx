/**
 * Sidebar component - Conversation list and actions
 */

interface Conversation {
  id: string
  name: string
  meta: string
  active?: boolean
}

interface SidebarProps {
  conversations: Conversation[]
  isOpen: boolean
  onToggle: () => void
  onNewConversation: () => void
  onClearAll: () => void
  onSelectConversation: (id: string) => void
}

export function Sidebar({
  conversations,
  isOpen,
  onToggle,
  onNewConversation,
  onClearAll,
  onSelectConversation,
}: SidebarProps) {
  return (
    <>
      {/* Mobile Menu Toggle */}
      <button
        id="sidebarToggle"
        className="sidebar-toggle"
        type="button"
        aria-label="Open conversation sidebar"
        aria-expanded={isOpen}
        onClick={onToggle}
      >
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M3 12h18M3 6h18M3 18h18" />
        </svg>
      </button>

      {/* Sidebar Panel */}
      <div className={`sidebar ${isOpen ? 'open' : ''}`}>
        <div className="sidebar-header">
          <h2>Conversations</h2>
        </div>
        <div className="sidebar-content">
          <button className="sidebar-button primary" onClick={onNewConversation}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 5v14M5 12h14" />
            </svg>
            New Conversation
          </button>
          <button className="sidebar-button danger" onClick={onClearAll}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14zM10 11v6M14 11v6" />
            </svg>
            Clear All
          </button>
          <div className="conversation-list">
            {conversations.length === 0 ? (
              <div className="conversation-item empty">
                <div className="conversation-name">No conversations yet</div>
                <div className="conversation-meta">Start your first conversation</div>
              </div>
            ) : (
              conversations.map((conv) => (
                <div
                  key={conv.id}
                  className={`conversation-item ${conv.active ? 'active' : ''}`}
                  onClick={() => onSelectConversation(conv.id)}
                >
                  <div className="conversation-name">{conv.name}</div>
                  <div className="conversation-meta">{conv.meta}</div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </>
  )
}
