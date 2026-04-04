/**
 * Header component - Longhouse brand + global nav + actions
 * Cyber/HUD aesthetic with glassmorphic controls
 */

interface NavItem {
  label: string
  href: string
}

const NAV_ITEMS: NavItem[] = [
  { label: 'Timeline', href: '/timeline' },
  { label: 'Oikos', href: '/chat' },
  { label: 'Integrations', href: '/settings/integrations' },
  { label: 'Machines', href: '/runners' },
]

interface HeaderProps {
  onReset?: () => void
  isResetting?: boolean
}

export function Header({ onReset, isResetting }: HeaderProps) {
  const currentPath = typeof window !== 'undefined' ? window.location.pathname : '/chat'

  return (
    <div className="main-header">
      <div className="header-brand">
        <a href="/timeline" className="brand-link">
          <div className="brand-logo-wrapper">
            <img
              src="/Gemini_Generated_Image_klhmhfklhmhfklhm-removebg-preview.png"
              alt=""
              className="brand-logo"
            />
            <div className="brand-logo-glow" aria-hidden="true" />
          </div>
          <h1>Longhouse</h1>
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
      </div>
    </div>
  )
}
