import { createContext, useCallback, useContext, useEffect, type ReactNode } from 'react';
import config from './config';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Navigate, useLocation } from 'react-router-dom';
import { toast } from 'react-hot-toast';
import {
  beginLogoutBarrier,
  cancelRefresh,
  clearLogoutBarrier,
  refreshAccessToken,
  RefreshUnavailableError,
} from './auth-refresh';
import { buildLoginUrl } from './loginRedirect';
import { requestNativeAuth, supportsNativeAuthBridge } from './nativeAuthBridge';
import { useServiceHealth, isServiceUnavailable } from './useServiceHealth';
import { ServiceUnavailable } from '../components/ServiceUnavailable';

// Types from our API
interface User {
  id: number;
  email: string;
  display_name?: string | null;
  avatar_url?: string | null;
  is_active: boolean;
  created_at: string;
  last_login?: string | null;
  prefs?: Record<string, unknown> | null;
  role?: string; // ADMIN or USER
}

interface TokenData {
  access_token: string;
  expires_in: number;
}

interface AuthContextType {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  authUnavailable: boolean;
  authRetryCount: number;
  login: (idToken: string) => Promise<TokenData>;
  logout: (everywhere?: boolean) => Promise<boolean>;
  refreshAuth: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);
export const CURRENT_USER_QUERY_KEY = ['current-user'] as const;
export const AUTH_METHODS_QUERY_KEY = ['auth-methods'] as const;
const AUTH_CHANNEL_NAME = 'longhouse-auth-events';
const LOGGED_OUT_SESSION_KEY = 'longhouse:logged-out';

export function hasLogoutIntent(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    if (window.localStorage.getItem(LOGGED_OUT_SESSION_KEY) === '1') return true;
  } catch {
    // Fall through to the per-tab fallback.
  }
  try {
    return window.sessionStorage.getItem(LOGGED_OUT_SESSION_KEY) === '1';
  } catch {
    return false;
  }
}

export function clearLogoutIntent(): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.removeItem(LOGGED_OUT_SESSION_KEY);
  } catch {
    // Storage can be disabled; the session fallback is still cleared.
  }
  try {
    window.sessionStorage.removeItem(LOGGED_OUT_SESSION_KEY);
  } catch {
    // Storage can be disabled; authenticated server state still wins.
  }
}

// Custom error class that includes HTTP status for retry logic
class HttpError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'HttpError';
    this.status = status;
  }
}

// API functions - all use credentials: 'include' for cookie auth
async function loginWithGoogle(idToken: string): Promise<{ access_token: string; expires_in: number }> {
  const response = await fetch(`${config.apiBaseUrl}/auth/google`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include', // Required for cookie to be set
    body: JSON.stringify({ id_token: idToken }),
  });

  if (!response.ok) {
    const error = await response.text();
    throw new HttpError(error || 'Login failed', response.status);
  }

  return response.json();
}

type AuthStatusResponse = {
  authenticated: boolean;
  user: User | null;
};

async function getCurrentUser(): Promise<User | null> {
  // A deliberate local sign-out is authoritative until the user explicitly
  // starts a new login. This prevents a failed remote revocation or a stale
  // cookie from silently signing the browser back in.
  if (typeof window !== 'undefined' && hasLogoutIntent()) {
    return null;
  }

  const response = await fetch(`${config.apiBaseUrl}/auth/status`, {
    credentials: 'include',
  });

  if (!response.ok) {
    throw new HttpError(`Failed to get auth status (${response.status})`, response.status);
  }

  const data = (await response.json()) as AuthStatusResponse;
  if (data.authenticated) {
    return data.user;
  }

  // A browser access cookie is intentionally short-lived. Share the same
  // single-flight refresh as API calls before treating the user as signed out.
  let refreshed: boolean;
  try {
    refreshed = await refreshAccessToken();
  } catch (error) {
    if (error instanceof RefreshUnavailableError) {
      throw new HttpError('Authentication service temporarily unavailable', 503);
    }
    throw error;
  }
  if (!refreshed) {
    return null;
  }

  const retryResponse = await fetch(`${config.apiBaseUrl}/auth/status`, {
    credentials: 'include',
  });
  if (!retryResponse.ok) {
    throw new HttpError(`Failed to get auth status (${retryResponse.status})`, retryResponse.status);
  }
  const retryData = (await retryResponse.json()) as AuthStatusResponse;
  return retryData.authenticated ? retryData.user : null;
}

async function logoutFromServer(everywhere = false): Promise<boolean> {
  try {
    const suffix = everywhere ? '?everywhere=1' : '';
    const response = await fetch(`${config.apiBaseUrl}/auth/logout${suffix}`, {
      method: 'POST',
      credentials: 'include', // Required to clear the cookie
      headers: { 'X-Longhouse-Auth': '1' },
    });
    if (!response.ok) {
      toast.error(`Could not complete logout (${response.status})`);
      return false;
    }
    return true;
  } catch {
    toast.error('Could not reach Longhouse to complete logout');
    return false;
  }
}

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  // Dev mode: auth disabled, no user
  if (!config.authEnabled) {
    const value: AuthContextType = {
      user: null,
      isAuthenticated: false,
      isLoading: false,
      authUnavailable: false,
      authRetryCount: 0,
      login: async () => ({ access_token: '', expires_in: 0 }),
      logout: async () => true,
      refreshAuth: async () => {},
    };

    return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
  }

  // Demo mode: synthetic user, no real auth
  if (config.demoMode) {
    const value: AuthContextType = {
      user: {
        id: 0,
        email: 'demo@longhouse.ai',
        display_name: 'Demo User',
        avatar_url: null,
        is_active: true,
        created_at: new Date().toISOString(),
        last_login: null,
        role: 'USER',
      },
      isAuthenticated: true,
      isLoading: false,
      authUnavailable: false,
      authRetryCount: 0,
      login: async () => ({ access_token: '', expires_in: 0 }),
      logout: async () => true,
      refreshAuth: async () => {},
    };

    return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
  }

  return <AuthProviderInner>{children}</AuthProviderInner>;
}

function AuthProviderInner({ children }: AuthProviderProps) {
  const queryClient = useQueryClient();
  const clearLocalAuth = useCallback(async () => {
    cancelRefresh();
    await queryClient.cancelQueries({ queryKey: CURRENT_USER_QUERY_KEY });
    queryClient.removeQueries({
      predicate: (query) => query.queryKey[0] !== CURRENT_USER_QUERY_KEY[0],
    });
    queryClient.setQueryData(CURRENT_USER_QUERY_KEY, null);
  }, [queryClient]);

  const notifyLogout = useCallback(() => {
    if (typeof window === 'undefined') return;
    window.dispatchEvent(new Event('longhouse-auth-logout'));
    try {
      // Every tab reads this durable intent before trusting a stale cookie.
      window.localStorage.setItem(LOGGED_OUT_SESSION_KEY, '1');
    } catch {
      // BroadcastChannel and the current tab still provide local protection.
    }
    if (typeof window.BroadcastChannel === 'function') {
      const channel = new BroadcastChannel(AUTH_CHANNEL_NAME);
      channel.postMessage({ type: 'logout' });
      channel.close();
    }
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const clearFromAnotherTab = (event?: StorageEvent) => {
      if (event && event.key !== LOGGED_OUT_SESSION_KEY) return;
      if (event?.newValue === null) {
        // Another tab explicitly started a new login. Do not turn that
        // user-initiated clear into another logout in this tab.
        try {
          window.sessionStorage.removeItem(LOGGED_OUT_SESSION_KEY);
        } catch {
          // Storage can be disabled; the next auth query will decide.
        }
        return;
      }
      try {
        window.sessionStorage.setItem(LOGGED_OUT_SESSION_KEY, '1');
      } catch {
        // The in-memory query state still clears.
      }
      void clearLocalAuth().then(() => {
        window.dispatchEvent(new Event('longhouse-auth-logout'));
      });
    };
    let channel: BroadcastChannel | null = null;
    if (typeof window.BroadcastChannel === 'function') {
      channel = new BroadcastChannel(AUTH_CHANNEL_NAME);
      channel.onmessage = (event) => {
        if (event.data?.type === 'logout') clearFromAnotherTab();
      };
    }
    window.addEventListener('storage', clearFromAnotherTab);
    return () => {
      channel?.close();
      window.removeEventListener('storage', clearFromAnotherTab);
    };
  }, [clearLocalAuth]);
  const {
    data: userData,
    isLoading,
    error: authError,
    failureCount: authRetryCount,
    refetch,
  } = useCurrentUserQuery();

  useEffect(() => {
    if (!userData) return;
    // A successful hosted handoff is a login too; it does not pass through
    // loginMutation, so clear the prior signed-out intent here.
    clearLogoutBarrier();
    clearLogoutIntent();
  }, [userData]);


  const loginMutation = useMutation({
    mutationFn: loginWithGoogle,
    onSuccess: async () => {
      clearLogoutBarrier();
      clearLogoutIntent();
      await refetch();
    },
    onError: (error: Error) => {
      toast.error(`Login failed: ${error.message}`);
    },
  });
  const login = async (idToken: string): Promise<TokenData> => {
    return loginMutation.mutateAsync(idToken);
  };
  const logout = async (everywhere = false): Promise<boolean> => {
    // Fence every tab before contacting the authority. Otherwise a refresh
    // response can install a fresh cookie after the logout response clears it.
    beginLogoutBarrier();
    const completed = await logoutFromServer(everywhere);
    if (!completed) {
      // Keep the authenticated UI and retry path when the authority could not
      // confirm revocation. The barrier must be cleared so normal requests can
      // continue while the user retries.
      clearLogoutBarrier();
      return false;
    }
    try {
      window.sessionStorage.setItem(LOGGED_OUT_SESSION_KEY, '1');
    } catch {
      // Storage can be disabled; the current tab still receives the event.
    }
    await clearLocalAuth();
    notifyLogout();
    return true;
  };

  const refreshAuth = async () => {
    // Refetch auth status from server
    await refetch();
  };

  const value: AuthContextType = {
    user: userData ?? null,
    isAuthenticated: Boolean(userData),
    isLoading,
    authUnavailable: isServiceUnavailable(authError),
    authRetryCount,
    login,
    logout,
    refreshAuth,
  };

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
export function useCurrentUserQuery() {
  return useQuery<User | null>({
    queryKey: CURRENT_USER_QUERY_KEY,
    queryFn: getCurrentUser,
    enabled: true,
    retry: (failureCount, err) => {
      if (isServiceUnavailable(err)) {
        return failureCount < 5;
      }
      return false;
    },
    retryDelay: (attemptIndex) => Math.min(1000 * Math.pow(2, attemptIndex), 10000),
    staleTime: 5 * 60 * 1000,
  });
}

function NativeAuthHandoff({ returnTo }: { returnTo: string }) {
  useEffect(() => {
    clearLogoutIntent();
    requestNativeAuth(returnTo);
  }, [returnTo]);

  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      height: '100vh', fontSize: '1.2rem',
      background: 'linear-gradient(135deg, #120B09 0%, #1A1410 100%)',
      color: 'rgba(243, 234, 217, 0.7)',
    }}>
      Returning to sign in...
    </div>
  );
}

// Global Google Sign-In SDK type augmentation (used by LoginPage)
declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: { client_id: string; callback: (response: { credential: string }) => void }) => void;
          renderButton: (element: HTMLElement, options: { theme: string; size: string }) => void;
        };
        oauth2?: {
          initCodeClient: (config: {
            client_id: string;
            scope: string;
            ux_mode: "popup";
            select_account?: boolean;
            callback: (response: { code?: string; error?: string; error_description?: string }) => void;
            error_callback?: (error: { type?: string; message?: string }) => void;
          }) => {
            requestCode: () => void;
          };
        };
      };
    };
  }
}

// Auth methods response type
export interface AuthMethods {
  google: boolean;
  password: boolean;
  sso: boolean;
  sso_url: string | null;
  sso_login_url?: string | null;
}
export function useAuthMethods() {
  return useQuery<AuthMethods>({
    queryKey: AUTH_METHODS_QUERY_KEY,
    queryFn: getAuthMethods,
    staleTime: 5 * 60 * 1000,
  });
}

// Fetch available authentication methods from the backend. A failed discovery
// request is a real service error, not evidence that this tenant is self-hosted:
// falling back to local methods can strand hosted users on a login spinner or
// send them down a disabled path.
async function getAuthMethods(): Promise<AuthMethods> {
  const response = await fetch(`${config.apiBaseUrl}/auth/methods`, {
    credentials: 'include',
  });
  if (!response.ok) {
    throw new HttpError(`Failed to discover authentication methods (${response.status})`, response.status);
  }
  return response.json();
}

// Auth guard component — redirects unauthenticated users to /login
interface AuthGuardProps {
  children: ReactNode;
  clientId?: string; // kept for API compat, unused after LoginOverlay removal
}

export function AuthGuard({ children }: AuthGuardProps) {
  const {
    isAuthenticated,
    isLoading,
    authUnavailable,
    authRetryCount,
    refreshAuth,
  } = useAuth();
  const { status: serviceStatus, retryCount, retry } = useServiceHealth();
  const location = useLocation();

  // Skip auth guard if authentication is not real (dev/demo modes)
  if (!config.authEnabled || config.demoMode) {
    return <>{children}</>;
  }

  // Show service unavailable screen when backend is not reachable
  if (serviceStatus === 'unavailable' || serviceStatus === 'checking') {
    if (serviceStatus === 'checking' && retryCount === 0) {
      return (
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          height: '100vh', fontSize: '1.2rem',
          background: 'linear-gradient(135deg, #120B09 0%, #1A1410 100%)',
          color: 'rgba(243, 234, 217, 0.7)',
        }}>
          Loading...
        </div>
      );
    }
    return <ServiceUnavailable retryCount={retryCount} onRetry={retry} />;
  }

  // A healthy tenant can still be unable to reach the CP refresh authority.
  // Keep that state distinct from anonymous so a transient outage never
  // redirects the user into a fresh login handoff.
  if (authUnavailable) {
    return <ServiceUnavailable retryCount={authRetryCount} onRetry={() => void refreshAuth()} />;
  }
  if (isLoading) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        height: '100vh', fontSize: '1.2rem',
        background: 'linear-gradient(135deg, #120B09 0%, #1A1410 100%)',
        color: 'rgba(243, 234, 217, 0.7)',
      }}>
        Loading...
      </div>
    );
  }

  if (!isAuthenticated) {
    const returnTo = location.pathname + location.search + location.hash;
    if (supportsNativeAuthBridge()) {
      return <NativeAuthHandoff returnTo={returnTo} />;
    }
    return <Navigate to={buildLoginUrl(returnTo)} replace />;
  }

  return <>{children}</>;
}
