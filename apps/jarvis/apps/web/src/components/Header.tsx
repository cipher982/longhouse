/**
 * Header component - App title and actions
 */

interface HeaderProps {
  title: string
  onSync: () => void
}

export function Header({ title, onSync }: HeaderProps) {
  return (
    <div className="main-header">
      <h1>{title}</h1>
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
        <button className="header-button" title="Sync conversations" onClick={onSync}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M23 4v6h-6M1 20v-6h6M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.64A9 9 0 0 1 3.51 15" />
          </svg>
          <span>Sync</span>
        </button>
      </div>
    </div>
  )
}
