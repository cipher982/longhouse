import clsx from "clsx";
import type { PropsWithChildren } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { useShelf } from "../lib/useShelfState";
import { useWebSocket, ConnectionStatusIndicator } from "../lib/useWebSocket";
import "../styles/layout.css";
import { MenuIcon } from "./icons";

const STATUS_ITEMS = [
  { label: "Runs", value: "0" },
  { label: "Cost", value: "--" },
  { label: "Err", value: "0" },
  { label: "Budget", value: "0%" },
];

function WelcomeHeader() {
  const { user, logout } = useAuth();
  const { isShelfOpen, toggleShelf } = useShelf();
  const location = useLocation();
  const navigate = useNavigate();

  // Only show shelf toggle on routes that have drawer UI
  const shouldShowShelfToggle =
    location.pathname.startsWith("/canvas") ||
    location.pathname.startsWith("/agent");

  // Generate user initials from display name or email
  const getUserInitials = (user: { display_name?: string | null; email: string } | null) => {
    if (!user) return "?";

    if (user.display_name) {
      // Get initials from display name
      const names = user.display_name.trim().split(/\s+/);
      if (names.length >= 2) {
        return (names[0][0] + names[names.length - 1][0]).toUpperCase();
      }
      return names[0][0].toUpperCase();
    }

    // Get initials from email
    const emailPrefix = user.email.split('@')[0];
    if (emailPrefix.length >= 2) {
      return (emailPrefix[0] + emailPrefix[1]).toUpperCase();
    }
    return emailPrefix[0].toUpperCase();
  };

  const userInitials = getUserInitials(user);

  const handleAvatarClick = () => {
    if (confirm("Do you want to log out?")) {
      logout();
    }
  };

  const navItems = [
    { label: 'Chat', href: '/chat', isExternal: true },
    { label: 'Dashboard', href: '/dashboard' },
    { label: 'Canvas', href: '/canvas' },
    { label: 'Integrations', href: '/settings/integrations' },
    { label: 'Runners', href: '/runners' },
  ];

  if (user?.role === 'ADMIN') {
    navItems.push({ label: 'Admin', href: '/admin', isExternal: false });
  }

  return (
    <header className="main-header" data-testid="welcome-header">
      <div className="header-left">
        {shouldShowShelfToggle && (
          <button
            id="shelf-toggle-btn"
            className="header-button shelf-toggle"
            aria-label="Open agent panel"
            aria-controls="agent-shelf"
            aria-expanded={isShelfOpen}
            onClick={toggleShelf}
          >
            <MenuIcon />
          </button>
        )}
        <div className="header-brand">
          <a href="/dashboard" className="brand-link" onClick={(e) => { e.preventDefault(); navigate('/dashboard'); }}>
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
      </div>

      <nav className="header-nav" aria-label="Main navigation">
        {navItems.map(({ label, href, isExternal }) => {
          const isActive =
            location.pathname === href ||
            (href !== '/' && location.pathname.startsWith(href))

          if (isExternal) {
            return (
              <a
                key={href}
                href={href}
                className={clsx("nav-tab", { "nav-tab--active": isActive })}
                aria-current={isActive ? 'page' : undefined}
              >
                <span className="nav-tab-label">{label}</span>
                {isActive && <span className="nav-tab-indicator" aria-hidden="true" />}
              </a>
            );
          }

          return (
            <button
              key={href}
              type="button"
              className={clsx("nav-tab", { "nav-tab--active": isActive })}
              aria-current={isActive ? 'page' : undefined}
              onClick={() => navigate(href)}
            >
              <span className="nav-tab-label">{label}</span>
              {isActive && <span className="nav-tab-indicator" aria-hidden="true" />}
            </button>
          );
        })}
      </nav>

      <div className="header-actions">
        <div className="user-menu-container">
          <div
            className="avatar-badge"
            aria-label="User avatar"
            role="button"
            tabIndex={0}
            onClick={handleAvatarClick}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                handleAvatarClick();
              }
            }}
            title="Click to log out"
          >
            {user?.avatar_url ? (
              <img
                src={user.avatar_url}
                alt="User avatar"
                className="avatar-img"
              />
            ) : (
              <span>{userInitials}</span>
            )}
          </div>
        </div>
      </div>
    </header>
  );
}

function StatusFooter() {
  // Use a background WebSocket connection for general status monitoring
  const { connectionStatus } = useWebSocket(true, {
    includeAuth: true,
    // Don't invalidate any queries from the layout level
    invalidateQueries: [],
  });

  return (
    <footer className="status-bar" data-testid="status-footer" aria-live="polite">
      <div className="packet-counter">
        <ConnectionStatusIndicator status={connectionStatus} />
      </div>
    </footer>
  );
}

export default function Layout({ children }: PropsWithChildren) {
  const location = useLocation();

  const isCanvasRoute = location.pathname.startsWith("/canvas");

  return (
    <>
      <WelcomeHeader />
      <div
        id="app-container"
        className={clsx({ "canvas-view": isCanvasRoute })}
        data-testid="app-container"
      >
        {children}
      </div>
      <StatusFooter />
    </>
  );
}
