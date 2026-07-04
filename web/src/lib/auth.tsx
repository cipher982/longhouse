import { createContext, useContext, useEffect, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Navigate, useLocation } from 'react-router-dom';
import { toast } from 'react-hot-toast';
import config from './config';
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
  login: (idToken: string) => Promise<TokenData>;
  logout: () => Promise<void>;
  refreshAuth: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);
export const CURRENT_USER_QUERY_KEY = ['current-user'] as const;
export const AUTH_METHODS_QUERY_KEY = ['auth-methods'] as const;

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
  const response = await fetch(`${config.apiBaseUrl}/auth/status`, {
    credentials: 'include', // Use cookie for auth
  });

  if (!response.ok) {
    throw new HttpError(`Failed to get auth status (${response.status})`, response.status);
  }

  const data = (await response.json()) as AuthStatusResponse;
  return data.authenticated ? data.user : null;
}

async function logoutFromServer(): Promise<void> {
  try {
    await fetch(`${config.apiBaseUrl}/auth/logout`, {
      method: 'POST',
      credentials: 'include', // Required to clear the cookie
    });
  } catch {
    // Ignore logout errors - user is logged out client-side anyway
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
      login: async () => ({ access_token: '', expires_in: 0 }),
      logout: async () => {},
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
      login: async () => ({ access_token: '', expires_in: 0 }),
      logout: async () => {},
      refreshAuth: async () => {},
    };

    return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
  }

  return <AuthProviderInner>{children}</AuthProviderInner>;
}

function AuthProviderInner({ children }: AuthProviderProps) {
  const queryClient = useQueryClient();

  const { data: userData, isLoading, refetch } = useCurrentUserQuery();

  const loginMutation = useMutation({
    mutationFn: loginWithGoogle,
    onSuccess: async () => {
      // Cookie is set by server; refetch user data
      await refetch();
    },
    onError: (error: Error) => {
      toast.error(`Login failed: ${error.message}`);
    },
  });

  const login = async (idToken: string): Promise<TokenData> => {
    const result = await loginMutation.mutateAsync(idToken);
    return result;
  };

  const logout = async () => {
    await logoutFromServer(); // Clear server-side cookie
    queryClient.removeQueries({
      predicate: (query) => query.queryKey[0] !== CURRENT_USER_QUERY_KEY[0],
    });
    queryClient.setQueryData(CURRENT_USER_QUERY_KEY, null);
  };

  const refreshAuth = async () => {
    // Refetch auth status from server
    await refetch();
  };

  const value: AuthContextType = {
    user: userData ?? null,
    isAuthenticated: Boolean(userData),
    isLoading,
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

// Fetch available authentication methods from the backend.
// Keep this internal to prevent components from bypassing the shared query hook.
async function getAuthMethods(): Promise<AuthMethods> {
  try {
    const response = await fetch(`${config.apiBaseUrl}/auth/methods`);
    if (!response.ok) {
      return {
        google: true,
        password: true,
        sso: false,
        sso_url: null,
        sso_login_url: null,
      };
    }
    return response.json();
  } catch {
    // Default to showing both on network errors
    return {
      google: true,
      password: true,
      sso: false,
      sso_url: null,
      sso_login_url: null,
    };
  }
}

// Auth guard component — redirects unauthenticated users to /login
interface AuthGuardProps {
  children: ReactNode;
  clientId?: string; // kept for API compat, unused after LoginOverlay removal
}

export function AuthGuard({ children }: AuthGuardProps) {
  const { isAuthenticated, isLoading } = useAuth();
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
