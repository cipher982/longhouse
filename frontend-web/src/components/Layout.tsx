import clsx from "clsx";
import type { PropsWithChildren } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { useWebSocket, ConnectionStatusIndicator } from "../lib/useWebSocket";
import "../styles/layout.css";

const STATUS_ITEMS = [
  { label: "Runs", value: "0" },
  { label: "Cost", value: "--" },
  { label: "Err", value: "0" },
  { label: "Budget", value: "0%" },
];

function WelcomeHeader() {
  const { user, logout } = useAuth();

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

  const displayName = user?.display_name || user?.email || "Unknown User";
  const userInitials = getUserInitials(user);

  const handleAvatarClick = () => {
    if (confirm("Do you want to log out?")) {
      logout();
    }
  };

  return (
    <header className="welcome-header" data-testid="welcome-header">
      <div className="welcome-copy">
        <span className="welcome-greeting">Welcome, {displayName}!</span>
        <div className="status-indicators" aria-label="System status">
          {STATUS_ITEMS.map((item) => (
            <span key={item.label} className="status-indicator">
              <span className="status-label">{item.label}:</span> {item.value}
            </span>
          ))}
        </div>
      </div>
      <div
        className="user-avatar"
        aria-label="User avatar"
        role="button"
        tabIndex={0}
        onClick={handleAvatarClick}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            handleAvatarClick();
          }
        }}
        style={{ cursor: 'pointer' }}
        title="Click to log out"
      >
        {user?.avatar_url ? (
          <img
            src={user.avatar_url}
            alt="User avatar"
            style={{
              width: '100%',
              height: '100%',
              borderRadius: '50%',
              objectFit: 'cover'
            }}
          />
        ) : (
          <span>{userInitials}</span>
        )}
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
    <footer className="status-footer" data-testid="status-footer" aria-live="polite">
      <span className="status-footer-label">Status:</span>
      <ConnectionStatusIndicator status={connectionStatus} />
    </footer>
  );
}

export default function Layout({ children }: PropsWithChildren) {
  const navigate = useNavigate();
  const location = useLocation();
  const { user } = useAuth();

  const isDashboardRoute =
    location.pathname === "/" || location.pathname.startsWith("/dashboard");
  const isCanvasRoute = location.pathname.startsWith("/canvas");
  const isProfileRoute = location.pathname.startsWith("/profile");
  const isAdminRoute = location.pathname.startsWith("/admin");

  // Check if user has admin access using authoritative role field
  const isAdmin = user?.role === 'ADMIN';

  const handleTabClick = (path: string) => {
    navigate(path);
  };

  return (
    <div
      id="app-container"
      className={clsx("app-shell", { "canvas-view": isCanvasRoute })}
      data-testid="app-container"
    >
      <WelcomeHeader />
      <nav id="global-tabs-container" className="tabs-container">
        <button
          id="global-dashboard-tab"
          type="button"
          data-testid="global-dashboard-tab"
          className={clsx("tab-button", { active: isDashboardRoute })}
          onClick={() => handleTabClick("/dashboard")}
        >
          Agent Dashboard
        </button>
        <button
          id="global-canvas-tab"
          type="button"
          data-testid="global-canvas-tab"
          className={clsx("tab-button", { active: isCanvasRoute })}
          onClick={() => handleTabClick("/canvas")}
        >
          Canvas Editor
        </button>
        <button
          id="global-profile-tab"
          type="button"
          data-testid="global-profile-tab"
          className={clsx("tab-button", { active: isProfileRoute })}
          onClick={() => handleTabClick("/profile")}
        >
          Profile
        </button>
        {isAdmin && (
          <button
            id="global-admin-tab"
            type="button"
            data-testid="global-admin-tab"
            className={clsx("tab-button", { active: isAdminRoute })}
            onClick={() => handleTabClick("/admin")}
          >
            Admin
          </button>
        )}
      </nav>
      <main id="main-content" className="main-content-area">
        {children}
      </main>
      <StatusFooter />
    </div>
  );
}
