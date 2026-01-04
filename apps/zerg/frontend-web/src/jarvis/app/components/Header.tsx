/**
 * Header component - Swarmlet brand + global nav + actions
 * Cyber/HUD aesthetic with glassmorphic controls
 */

interface NavItem {
  label: string
  href: string
}

const NAV_ITEMS: NavItem[] = [
  { label: 'Chat', href: '/chat' },
  { label: 'Dashboard', href: '/dashboard' },
  { label: 'Canvas', href: '/canvas' },
  { label: 'Integrations', href: '/settings/integrations' },
  { label: 'Runners', href: '/runners' },
]

interface HeaderProps {
  onSync: () => void
  onReset?: () => void
  isResetting?: boolean
}

export function Header({ onSync, onReset, isResetting }: HeaderProps) {
  const currentPath = typeof window !== 'undefined' ? window.location.pathname : '/chat'

  return (
    <div className="main-header">
      <div className="header-brand">
        <a href="/chat" className="brand-link">
          <div className="brand-logo-wrapper">
            <img
              src="/Gemini_Generated_Image_klhmhfklhmhfklhm-removebg-preview.png"
              alt=""
              className="brand-logo"
            />
            <div className="brand-logo-glow" aria-hidden="true" />
          </div>
          <h1>Swarmlet</h1>
        </a>
      </div>

      <nav className="header-nav" aria-label="Main navigation">
        {NAV_ITEMS.map(({ label, href }) => {
          const isActive =
            currentPath === href ||
            (href !== '/' && currentPath.startsWith(href)) ||
            (href === '/chat' && (currentPath === '/' || currentPath === '/chat'))

          return (
            <a
              key={href}
              href={href}
              className={`nav-tab ${isActive ? 'nav-tab--active' : ''}`}
              aria-current={isActive ? 'page' : undefined}
            >
              <span className="nav-tab-label">{label}</span>
              {isActive && <span className="nav-tab-indicator" aria-hidden="true" />}
            </a>
          )
        })}
      </nav>

      <div className="header-actions">
        {onReset && (
          <button
            className="header-button header-button-reset"
            title="Reset memory (clear chat history)"
            onClick={onReset}
            disabled={isResetting}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14zM10 11v6M14 11v6" />
            </svg>
            <span>{isResetting ? 'Resetting...' : 'Reset'}</span>
          </button>
        )}
        <button className="header-button" title="Sync conversations" onClick={onSync}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M23 4v6h-6M1 20v-6h6M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.64A9 9 0 0 1 3.51 15" />
          </svg>
          <span>Sync</span>
        </button>
      </div>
    </div>
  )
}
