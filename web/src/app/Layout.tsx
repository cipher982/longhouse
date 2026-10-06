import clsx from "clsx";
import { useState, useCallback, useEffect, useRef, type PropsWithChildren } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { useAuth, useAuthMethods } from "@/features/auth/auth";
import { buildLoginUrl } from "@/features/auth/loginRedirect";
import { clearLogoutBarrier } from "@/features/auth/auth-refresh";
import { requestNativeAuth } from "@/features/auth/nativeAuthBridge";
import { useApiHealth } from "./apiHealth";
import { useBodyScrollLock } from "@/shared/hooks/useBodyScrollLock";
import { useClickOutside } from "@/shared/hooks/useClickOutside";
import { useDocumentVisible } from "@/shared/hooks/useDocumentVisible";
import { useEscapeKey } from "@/shared/hooks/useEscapeKey";
import { useWebClientPresence } from "./useWebClientPresence";
import { useConfirm } from "@/shared/ui/confirm";
import { useMachineDirectory } from "@/features/machines/useMachines";
import { SwarmLogo } from "@/shared/ui/SwarmLogo";
import "./styles/layout.css";
import { XIcon } from "@/shared/ui/icons";
import { getNavItems } from "./navigation/navItems";
import { HeaderSlotContext, MobileNavSlotContext, isSessionRoute } from "./headerSlot";

const MACHINE_STATUS_INITIAL_DELAY_MS = 2_500;

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

function WelcomeHeader({
  compact = false,
  slotRef,
  mobileSlotRef,
}: {
  /** Session routes: one ~44px bar whose middle is the page's slot. */
  compact?: boolean;
  slotRef?: (node: HTMLDivElement | null) => void;
  mobileSlotRef?: (node: HTMLDivElement | null) => void;
}) {
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

  const navItems = getNavItems();

  return (
    <>
    <header
      className={clsx("main-header", { "main-header--session": compact })}
      data-testid="welcome-header"
    >
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

      {compact ? (
        <div className="header-session-slot" ref={slotRef} data-testid="header-session-slot" />
      ) : null}

      <div className="header-actions">
        <NavStatus compact={compact} />
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
      {compact ? (
        <div
          className="mobile-nav-session-slot"
          ref={mobileSlotRef}
          data-testid="mobile-nav-session-slot"
        />
      ) : null}
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

// Folded into the nav's right cluster. It says how many enrolled machines hold
// a live connection, from the machine directory (never the optional Runner
// count), and only names the API when a tracked request got no answer or a
// server error (see apiHealth.ts for why a 4xx never counts).
function NavStatus({ compact = false }: { compact?: boolean }) {
  const documentVisible = useDocumentVisible();
  const [queryEnabled, setQueryEnabled] = useState(false);
  const apiError = useApiHealth();

  useEffect(() => {
    if (!documentVisible) {
      return;
    }

    const timerId = window.setTimeout(() => {
      setQueryEnabled(true);
    }, MACHINE_STATUS_INITIAL_DELAY_MS);

    return () => {
      window.clearTimeout(timerId);
    };
  }, [documentVisible]);

  const { data: directory, dataUpdatedAt, isError: directoryUnavailable, error: directoryError } = useMachineDirectory({
    enabled: queryEnabled,
    refetchInterval: documentVisible ? 30_000 : false,
  });

  const machines = directory?.machines ?? [];
  const online = machines.filter((machine) => machine.online).length;
  if (!apiError && !directoryUnavailable && machines.length === 0) return null;

  const label = apiError
    ? apiError.label
    : directoryUnavailable
      ? directory ? `${online} of ${machines.length} machines online (last known)` : "Machine status unavailable"
      : `${online} of ${machines.length} ${machines.length === 1 ? "machine" : "machines"} online`;
  const title = apiError
    ? apiError.detail
    : directoryUnavailable
      ? `${directoryError instanceof Error ? directoryError.message : "The machine directory could not be refreshed."}${directory ? `\nLast known at ${new Date(dataUpdatedAt).toLocaleTimeString()}.` : ""}`
      : machines.map((machine) => `${machine.machine_name}: ${machine.online ? "online" : "offline"}`).join("\n");

  return (
    <Link
      to="/machines"
      className={clsx("nav-status", { "nav-status--compact": compact })}
      data-testid="nav-status"
      title={compact ? `${label}\n${title}` : title}
      aria-live="polite"
    >
      <span
        className={clsx("nav-status-dot", {
          "nav-status-dot--error": Boolean(apiError),
          "nav-status-dot--off": !apiError && (directoryUnavailable || online === 0),
        })}
        aria-hidden="true"
      />
      {/* The compact bar keeps the count, not the sentence; the session page
          no longer repeats it anywhere else. */}
      {compact && !apiError && !directoryUnavailable ? (
        <>
          <span aria-hidden="true">{online}/{machines.length}</span>
          <span className="sr-only">{label}</span>
        </>
      ) : (
        label
      )}
    </Link>
  );
}

export default function Layout({ children }: PropsWithChildren) {
  useWebClientPresence();
  const location = useLocation();
  const compact = isSessionRoute(location.pathname);
  const [slot, setSlot] = useState<HTMLDivElement | null>(null);
  const [mobileSlot, setMobileSlot] = useState<HTMLDivElement | null>(null);

  return (
    <HeaderSlotContext.Provider value={compact ? slot : null}>
      <MobileNavSlotContext.Provider value={compact ? mobileSlot : null}>
        <WelcomeHeader compact={compact} slotRef={setSlot} mobileSlotRef={setMobileSlot} />
        <div
          id="app-container"
          data-testid="app-container"
        >
          {children}
        </div>
      </MobileNavSlotContext.Provider>
    </HeaderSlotContext.Provider>
  );
}
