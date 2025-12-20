/**
 * Header component - Swarmlet brand + global nav + actions
 * Cyber/HUD aesthetic with glassmorphic controls
 */

interface NavItem {
  label: string
  href: string
  isActive?: boolean
}

const NAV_ITEMS: NavItem[] = [
  { label: 'Chat', href: '/chat', isActive: true },
  { label: 'Dashboard', href: '/dashboard' },
  { label: 'Canvas', href: '/canvas' },
  { label: 'Integrations', href: '/settings/integrations' },
  { label: 'Runners', href: '/runners' },
]

interface HeaderProps {
  onSync: () => void
}

export function Header({ onSync }: HeaderProps) {
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
        {NAV_ITEMS.map(({ label, href, isActive }) => (
          <a
            key={href}
            href={href}
            className={`nav-tab ${isActive ? 'nav-tab--active' : ''}`}
            aria-current={isActive ? 'page' : undefined}
          >
            <span className="nav-tab-label">{label}</span>
            {isActive && <span className="nav-tab-indicator" aria-hidden="true" />}
          </a>
        ))}
      </nav>

      <div className="header-actions">
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
