import clsx from "clsx";
import { useState, useCallback, useEffect, useRef, type PropsWithChildren } from "react";
import { useLocation, useNavigate } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { useAuth, useAuthMethods } from "../lib/auth";
import { buildLoginUrl } from "../lib/loginRedirect";
import { clearLogoutBarrier } from "../lib/auth-refresh";
import { requestNativeAuth } from "../lib/nativeAuthBridge";
import { useApiHealth } from "../lib/apiHealth";
import { useBodyScrollLock } from "../hooks/useBodyScrollLock";
import { useClickOutside } from "../hooks/useClickOutside";
import { useDocumentVisible } from "../hooks/useDocumentVisible";
import { useEscapeKey } from "../hooks/useEscapeKey";
import { useWebClientPresence } from "../hooks/useWebClientPresence";
import { useConfirm } from "./confirm";
import { fetchRunnerStatus } from "../services/api";
import { SwarmLogo } from "./SwarmLogo";
import "../styles/layout.css";
import { XIcon } from "./icons";
import { getNavItems } from "./navigation/navItems";

const RUNNER_STATUS_INITIAL_DELAY_MS = 2_500;

type AvatarUser = { avatar_url?: string | null } | null | undefined;

// A broken/unreachable avatar_url must never fall back to the browser's own
// <img alt> rendering — that renders as overflowing text inside the small
// circle. Track load failures per URL and fall back to initials instead.
function AvatarContent({ user, initials, className }: { user: AvatarUser; initials: string; className?: string }) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const avatarUrl = user?.avatar_url ?? null;

  if (avatarUrl && avatarUrl !== failedUrl) {
    return (
      <img
        src={avatarUrl}
        alt=""
        className={className}
        onError={() => setFailedUrl(avatarUrl)}
      />
    );
  }
  return <span>{initials}</span>;
}

function WelcomeHeader() {
  const { user, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const confirm = useConfirm();
  const [mobileNavState, setMobileNavState] = useState({
    open: false,
    pathname: location.pathname,
  });
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const userMenuRef = useRef<HTMLDivElement>(null);
  const { data: authMethods } = useAuthMethods();
  const mobileNavOpen =
    mobileNavState.pathname === location.pathname && mobileNavState.open;
  const closeMobileNav = useCallback(() => {
    setMobileNavState({ open: false, pathname: location.pathname });
  }, [location.pathname]);
  const toggleMobileNav = useCallback(() => {
    setMobileNavState((previous) => {
      if (previous.pathname !== location.pathname) {
        return { open: true, pathname: location.pathname };
      }
      return { open: !previous.open, pathname: location.pathname };
    });
  }, [location.pathname]);
  const closeUserMenu = useCallback(() => setUserMenuOpen(false), []);
  const toggleUserMenu = useCallback(() => setUserMenuOpen(prev => !prev), []);
  const handleOpenSettings = useCallback(() => {
    closeUserMenu();
    navigate("/settings");
  }, [closeUserMenu, navigate]);

  useEscapeKey(() => {
    closeMobileNav();
  }, mobileNavOpen);
  useBodyScrollLock(mobileNavOpen);
  useClickOutside({
    enabled: userMenuOpen,
    refs: [userMenuRef],
    onClickOutside: closeUserMenu,
  });

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

  const controlPlaneBase = authMethods?.sso_url ? authMethods.sso_url.replace(/\/+$/, "") : null;

  const controlPlaneLogoutUrl = (returnTo: string): string | null => {
    if (!controlPlaneBase) return null;
    const target = new URL('/auth/logout', `${controlPlaneBase}/`);
    target.searchParams.set('return_to', returnTo);
    return target.toString();
  };


  const handleLogout = async () => {
    const confirmed = await confirm({
      title: 'Log out?',
      message: 'You will need to sign in again to access this instance.',
      confirmLabel: 'Log out',
      cancelLabel: 'Stay signed in',
      variant: 'default',
    });
    if (!confirmed) return;
    closeUserMenu();
    const completed = await logout();
    if (!completed) return;
    const returnTo = window.location.pathname + window.location.search + window.location.hash;
    if (!requestNativeAuth(returnTo)) {
      window.location.replace(buildLoginUrl(returnTo));
    }
  };

  const handleLogoutEverywhere = async () => {
    const confirmed = await confirm({
      title: 'Log out everywhere?',
      message: 'This signs you out of this instance and the control plane.',
      confirmLabel: 'Log out everywhere',
      cancelLabel: 'Cancel',
      variant: 'warning',
    });
    if (!confirmed) return;
    closeUserMenu();
    const completed = await logout(true);
    if (!completed) return;
    const returnTo = window.location.pathname + window.location.search + window.location.hash;
    const cpLogoutUrl = controlPlaneLogoutUrl(returnTo);
    if (cpLogoutUrl) {
      // Keep the refresh barrier and signed-out intent active through CP
      // logout. The next explicit login clears both after handoff.
      window.location.assign(cpLogoutUrl);
    } else {
      clearLogoutBarrier();
    }
  };

  const handleSwitchAccount = async () => {
    const confirmed = await confirm({
      title: 'Switch account?',
      message: 'You will be redirected to sign in with a different account.',
      confirmLabel: 'Switch account',
      cancelLabel: 'Cancel',
      variant: 'default',
    });
    if (!confirmed) return;
    closeUserMenu();
    const completed = await logout(true);
    if (!completed) return;
    const returnTo = window.location.pathname + window.location.search + window.location.hash;
    const cpLogoutUrl = controlPlaneLogoutUrl(returnTo);
    if (cpLogoutUrl) {
      // Keep the refresh barrier and signed-out intent active through CP
      // logout. The next explicit login clears both after handoff.
      window.location.assign(cpLogoutUrl);
    } else {
      clearLogoutBarrier();
    }
  };

  const navItems = getNavItems(user?.role);

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
          <a href="/timeline" className="brand-link" onClick={(e) => { e.preventDefault(); navigate('/timeline'); }}>
            <SwarmLogo size={24} className="brand-logo" />
            <h1>Longhouse</h1>
          </a>
        </div>
      </div>

      <nav className="header-nav" aria-label="Main navigation">
        {navItems.map(({ label, href, testId }) => {
          const isActive =
            location.pathname === href ||
            (href !== '/' && location.pathname.startsWith(href))

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
        <NavStatus />
        <div className="user-menu-container" ref={userMenuRef}>
          <div
            className="avatar-badge"
            aria-label="User menu"
            role="button"
            tabIndex={0}
            onClick={toggleUserMenu}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                toggleUserMenu();
              }
            }}
            title="Account menu"
          >
            <AvatarContent user={user} initials={userInitials} className="avatar-img" />
          </div>
          <div className={`user-dropdown ${userMenuOpen ? "" : "hidden"}`}>
            <button type="button" className="user-menu-item" onClick={handleOpenSettings}>
              Settings
            </button>
            {/*
              For hosted tenants, the single "Log out" button clears both
              the tenant session and the CP session in one shot, so the
              SSO bridge can't silently re-sign the user in. For self-host
              tenants there is no CP, so we use the same handler without
              the CP-clearing step.
            */}
            <button
              type="button"
              className="user-menu-item"
              onClick={controlPlaneBase ? handleLogoutEverywhere : handleLogout}
            >
              Log out
            </button>
            {controlPlaneBase && (
              <button type="button" className="user-menu-item" onClick={handleSwitchAccount}>
                Switch account
              </button>
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
      aria-hidden={!mobileNavOpen}
    >
      <div className="mobile-nav-header">
        <div className="mobile-nav-brand">
          <SwarmLogo size={24} />
          <span>Longhouse</span>
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
        {user && (
          <button
            type="button"
            className={clsx("mobile-nav-link", { "mobile-nav-link--active": location.pathname.startsWith("/settings") })}
            aria-current={location.pathname.startsWith("/settings") ? "page" : undefined}
            onClick={() => {
              navigate("/settings");
              closeMobileNav();
            }}
          >
            Settings
          </button>
        )}
      </div>
      {user && (
        <div className="mobile-nav-footer">
          <div className="mobile-nav-user">
            <div className="mobile-nav-avatar">
              <AvatarContent user={user} initials={userInitials} />
            </div>
            <div className="mobile-nav-user-info">
              <span className="mobile-nav-user-name">{user.display_name || user.email}</span>
              {user.display_name && user.email && user.display_name !== user.email && (
                <span className="mobile-nav-user-email">{user.email}</span>
              )}
            </div>
          </div>
          <button
            type="button"
            className="mobile-nav-logout"
            onClick={async () => {
              closeMobileNav();
              if (controlPlaneBase) {
                await handleLogoutEverywhere();
              } else {
                await handleLogout();
              }
            }}
          >
            Log out
          </button>
          {controlPlaneBase && (
            <button
              type="button"
              className="mobile-nav-logout"
              onClick={async () => {
                closeMobileNav();
                await handleSwitchAccount();
              }}
            >
              Switch account
            </button>
          )}
        </div>
      )}
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

// Folded into the nav's right cluster (was a separate footer status bar).
// Same data sources as before: useApiHealth for API reachability, the
// runnerStatus query for the machine count — just rendered as one sentence.
function NavStatus() {
  const documentVisible = useDocumentVisible();
  const [queryEnabled, setQueryEnabled] = useState(false);
  const apiError = useApiHealth();

  useEffect(() => {
    if (!documentVisible) {
      return;
    }

    const timerId = window.setTimeout(() => {
      setQueryEnabled(true);
    }, RUNNER_STATUS_INITIAL_DELAY_MS);

    return () => {
      window.clearTimeout(timerId);
    };
  }, [documentVisible]);

  const { data: runnerStatus } = useQuery({
    queryKey: ["runnerStatus"],
    queryFn: fetchRunnerStatus,
    enabled: queryEnabled,
    refetchInterval: documentVisible ? 30000 : false,
    staleTime: 15000,
    retry: false, // Don't retry on failure - just show stale data
  });

  const healthy = !apiError;
  const label = healthy ? "API healthy" : "API degraded";
  const machinesLabel =
    runnerStatus && runnerStatus.total > 0
      ? `, ${runnerStatus.online} of ${runnerStatus.total} machines up`
      : "";
  // Per-machine breakdown (name: status, one per line) lives on the title
  // attribute — folded in from the old RunnerStatusIndicator tooltip, which
  // this sentence replaced without carrying the detail forward.
  const runnersTitle =
    runnerStatus && runnerStatus.runners.length > 0
      ? runnerStatus.runners.map((r) => `${r.name}: ${r.status}`).join("\n")
      : null;
  const title = apiError ? apiError.message : (runnersTitle ?? "API responding normally");

  return (
    <span className="nav-status" data-testid="nav-status" title={title} aria-live="polite">
      <span
        className={clsx("nav-status-dot", { "nav-status-dot--error": !healthy })}
        aria-hidden="true"
      />
      {label}
      {machinesLabel}
    </span>
  );
}

export default function Layout({ children }: PropsWithChildren) {
  useWebClientPresence();

  return (
    <>
      <WelcomeHeader />
      <div
        id="app-container"
        data-testid="app-container"
      >
        {children}
      </div>
    </>
  );
}
