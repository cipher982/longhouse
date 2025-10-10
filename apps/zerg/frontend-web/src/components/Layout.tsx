import clsx from "clsx";
import type { PropsWithChildren } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
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
    <header className="header" data-testid="welcome-header">
      <button
        id="shelf-toggle-btn"
        aria-label="Open agent panel"
        aria-controls="agent-shelf"
        aria-expanded="false"
        onClick={() => {
          // TODO: Implement shelf toggle functionality
          console.log("Shelf toggle clicked - not implemented yet");
        }}
      >
        <MenuIcon />
      </button>
      <h1 id="header-title">AI Agent Platform</h1>
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
    </header>
  );
}

function StatusFooter() {
  // Use a background WebSocket connection for general status monitoring
  // TEMPORARILY DISABLED: Backend requires PostgreSQL but SQLite is configured
  const { connectionStatus } = useWebSocket(false, {
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
    <>
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
