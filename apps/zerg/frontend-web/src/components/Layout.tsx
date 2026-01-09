import clsx from "clsx";
import { useState, useEffect, useCallback, type PropsWithChildren } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { useShelf } from "../lib/useShelfState";
import { useWebSocket, ConnectionStatusIndicator } from "../lib/useWebSocket";
import { useConfirm } from "./confirm";
import "../styles/layout.css";
import { SidebarIcon, XIcon } from "./icons";

function WelcomeHeader() {
  const { user, logout } = useAuth();
  const { isShelfOpen, toggleShelf } = useShelf();
  const location = useLocation();
  const navigate = useNavigate();
  const confirm = useConfirm();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  // Close mobile nav on route change
  useEffect(() => {
    setMobileNavOpen(false);
  }, [location.pathname]);

  // Close mobile nav on escape key
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && mobileNavOpen) {
        setMobileNavOpen(false);
      }
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [mobileNavOpen]);

  // Prevent body scroll when mobile nav is open
  useEffect(() => {
    if (mobileNavOpen) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [mobileNavOpen]);

  const closeMobileNav = useCallback(() => setMobileNavOpen(false), []);
  const toggleMobileNav = useCallback(() => setMobileNavOpen(prev => !prev), []);

  // Only show shelf toggle on routes that have drawer UI
  const SHELF_ENABLED_ROUTES = ["/canvas", "/agent"];
  const shouldShowShelfToggle = SHELF_ENABLED_ROUTES.some(route =>
    location.pathname.startsWith(route)
  );

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

  const handleAvatarClick = async () => {
    const confirmed = await confirm({
      title: 'Log out?',
      message: 'You will need to sign in again to access your account.',
      confirmLabel: 'Log out',
      cancelLabel: 'Stay signed in',
      variant: 'default',
    });
    if (confirmed) {
      logout();
    }
  };

  const navItems = [
    { label: 'Chat', href: '/chat' },
    { label: 'Dashboard', href: '/dashboard' },
    { label: 'Canvas', href: '/canvas' },
    { label: 'Integrations', href: '/settings/integrations' },
    { label: 'Runners', href: '/runners' },
  ];

  if (user?.role === 'ADMIN') {
    navItems.push({ label: 'Admin', href: '/admin' });
  }

  return (
    <>
    <header className="main-header" data-testid="welcome-header">
      <div className="header-left">
        {/* Mobile hamburger menu - shown only on mobile via CSS */}
        <button
          className="mobile-menu-toggle"
          aria-label={mobileNavOpen ? "Close menu" : "Open menu"}
          aria-expanded={mobileNavOpen}
          aria-controls="mobile-nav-drawer"
          onClick={toggleMobileNav}
        >
          <span className="hamburger-icon">
            <span />
            <span />
            <span />
          </span>
        </button>

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
        {navItems.map(({ label, href }) => {
          const isActive =
            location.pathname === href ||
            (href !== '/' && location.pathname.startsWith(href))

          // Generate testid from label, e.g., "Canvas" -> "global-canvas-tab"
          const testId = `global-${label.toLowerCase()}-tab`;

          return (
            <button
              key={href}
              type="button"
              data-testid={testId}
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
        {shouldShowShelfToggle && (
          <button
            id="shelf-toggle-btn"
            className="header-button shelf-toggle"
            aria-label="Toggle agent panel"
            aria-controls="agent-shelf"
            aria-expanded={isShelfOpen}
            onClick={toggleShelf}
          >
            <SidebarIcon />
          </button>
        )}
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

    {/* Mobile navigation drawer */}
    <nav
      id="mobile-nav-drawer"
      className={clsx("mobile-nav-drawer", { open: mobileNavOpen })}
      aria-label="Mobile navigation"
    >
      <div className="mobile-nav-header">
        <div className="mobile-nav-brand">
          <img
            src="/Gemini_Generated_Image_klhmhfklhmhfklhm-removebg-preview.png"
            alt=""
          />
          <span>Swarmlet</span>
        </div>
        <button
          className="mobile-nav-close"
          aria-label="Close menu"
          onClick={closeMobileNav}
        >
          <XIcon width={20} height={20} />
        </button>
      </div>
      <div className="mobile-nav-links">
        {navItems.map(({ label, href }) => {
          const isActive =
            location.pathname === href ||
            (href !== '/' && location.pathname.startsWith(href));

          return (
            <button
              key={href}
              type="button"
              className={clsx("mobile-nav-link", { "mobile-nav-link--active": isActive })}
              aria-current={isActive ? 'page' : undefined}
              onClick={() => {
                navigate(href);
                closeMobileNav();
              }}
            >
              {label}
            </button>
          );
        })}
      </div>
    </nav>

    {/* Scrim overlay */}
    <div
      className={clsx("mobile-nav-scrim", { visible: mobileNavOpen })}
      onClick={closeMobileNav}
      aria-hidden="true"
    />
    </>
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
