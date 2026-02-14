import React, { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'react-hot-toast';
import config from './config';
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
  logout: () => void;
  getToken: () => string | null;
  refreshAuth?: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

// Legacy localStorage key - kept for migration/cleanup only
const LEGACY_TOKEN_STORAGE_KEY = 'zerg_jwt';

/**
 * Clean up legacy localStorage token if present.
 * Called on app init to migrate from localStorage to cookie auth.
 */
function cleanupLegacyToken(): void {
  try {
    localStorage.removeItem(LEGACY_TOKEN_STORAGE_KEY);
  } catch {
    // Ignore storage errors
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
      logout: () => {},
      getToken: () => null,
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
      logout: () => {},
      getToken: () => null,
    };

    return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
  }

  return <AuthProviderInner>{children}</AuthProviderInner>;
}

function AuthProviderInner({ children }: AuthProviderProps) {
  const [user, setUser] = useState<User | null>(null);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const queryClient = useQueryClient();

  // Clean up any legacy localStorage token on mount
  useEffect(() => {
    cleanupLegacyToken();
  }, []);

  // Check auth status via cookie on mount (always enabled - cookie determines auth)
  const { data: userData, isLoading, error, refetch } = useQuery<User | null>({
    queryKey: ['current-user'],
    queryFn: getCurrentUser,
    enabled: true, // Always try - cookie auth is checked server-side
    // Retry on 502/503 (service unavailable) but not on 401 (auth required)
    retry: (failureCount, err) => {
      if (isServiceUnavailable(err)) {
        return failureCount < 5; // Retry up to 5 times for service unavailable
      }
      return false; // Don't retry auth failures
    },
    retryDelay: (attemptIndex) => Math.min(1000 * Math.pow(2, attemptIndex), 10000),
    staleTime: 5 * 60 * 1000, // 5 minutes
  });

  const loginMutation = useMutation({
    mutationFn: loginWithGoogle,
    onSuccess: () => {
      // Cookie is set by server; refetch user data
      refetch();
    },
    onError: (error: Error) => {
      toast.error(`Login failed: ${error.message}`);
    },
  });

  useEffect(() => {
    if (userData) {
      setUser(userData);
      setIsAuthenticated(true);
      return;
    }

    if (userData === null || error) {
      setUser(null);
      setIsAuthenticated(false);
    }
  }, [userData, error]);

  const login = async (idToken: string): Promise<TokenData> => {
    const result = await loginMutation.mutateAsync(idToken);
    return result;
  };

  const logout = async () => {
    await logoutFromServer(); // Clear server-side cookie
    setUser(null);
    setIsAuthenticated(false);
    queryClient.clear();
  };

  const getToken = () => {
    // Deprecated: tokens are now in HttpOnly cookies (not JS-accessible)
    // Kept for API compatibility but always returns null
    return null;
  };

  const refreshAuth = async () => {
    // Refetch auth status from server
    await refetch();
  };

  const value: AuthContextType = {
    user,
    isAuthenticated,
    isLoading,
    login,
    logout,
    getToken,
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

// Google Sign-In component
interface GoogleSignInButtonProps {
  clientId: string;
  onSuccess?: () => void;
  onError?: (error: string) => void;
}

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: { client_id: string; callback: (response: { credential: string }) => void }) => void;
          renderButton: (element: HTMLElement, options: { theme: string; size: string }) => void;
        };
      };
    };
  }
}

export function GoogleSignInButton({ clientId, onSuccess, onError }: GoogleSignInButtonProps) {
  const { login } = useAuth();
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    // Load Google Sign-In script
    const script = document.createElement('script');
    script.src = 'https://accounts.google.com/gsi/client';
    script.async = true;
    script.defer = true;
    document.head.appendChild(script);

    script.onload = () => {
      if (window.google?.accounts?.id) {
        window.google.accounts.id.initialize({
          client_id: clientId,
          callback: async (response) => {
            setIsLoading(true);
            try {
              await login(response.credential);
              onSuccess?.();
            } catch (error) {
              const errorMessage = error instanceof Error ? error.message : 'Login failed';
              onError?.(errorMessage);
            } finally {
              setIsLoading(false);
            }
          },
        });

        const buttonDiv = document.getElementById('google-signin-button');
        if (buttonDiv) {
          window.google.accounts.id.renderButton(buttonDiv, {
            theme: 'outline',
            size: 'large',
          });
        }
      }
    };

    return () => {
      script.remove();
    };
  }, [clientId, login, onSuccess, onError]);

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
      <div id="google-signin-button" />
      {isLoading && <div>Signing in...</div>}
    </div>
  );
}

// Auth methods response type
export interface AuthMethods {
  google: boolean;
  password: boolean;
  sso: boolean;
  sso_url: string | null;
}

export interface PasswordLoginResult {
  ok: boolean;
  error?: string;
}

// Fetch available authentication methods from the backend
export async function getAuthMethods(): Promise<AuthMethods> {
  try {
    const response = await fetch(`${config.apiBaseUrl}/auth/methods`);
    if (!response.ok) {
      return { google: true, password: true, sso: false, sso_url: null };
    }
    return response.json();
  } catch {
    // Default to showing both on network errors
    return { google: true, password: true, sso: false, sso_url: null };
  }
}

// Password authentication
export async function loginWithPassword(password: string): Promise<PasswordLoginResult> {
  const response = await fetch(`${config.apiBaseUrl}/auth/password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ password }),
  });
  if (response.ok) {
    return { ok: true };
  }

  if (response.status === 400) {
    return { ok: false, error: 'Password auth not configured' };
  }

  if (response.status === 429) {
    const retryAfter = response.headers.get('Retry-After');
    const suffix = retryAfter ? ` Try again in ${retryAfter}s.` : ' Try again later.';
    return { ok: false, error: `Too many attempts.${suffix}` };
  }

  return { ok: false, error: 'Invalid password' };
}

// Dev login function (bypasses Google OAuth in development)
async function loginWithDevAccount(): Promise<{ access_token: string; expires_in: number }> {
  const response = await fetch(`${config.apiBaseUrl}/auth/dev-login`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include', // Required for cookie to be set
  });

  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || 'Dev login failed');
  }

  return response.json();
}

// Login overlay component
interface LoginOverlayProps {
  clientId: string;
}

export function LoginOverlay({ clientId }: LoginOverlayProps) {
  const [authMethods, setAuthMethods] = useState<AuthMethods | null>(null);
  const [password, setPassword] = useState('');
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [isPasswordLoading, setIsPasswordLoading] = useState(false);
  const [isDevLoginLoading, setIsDevLoginLoading] = useState(false);

  const [ssoRedirecting, setSsoRedirecting] = useState(false);

  useEffect(() => {
    getAuthMethods().then(setAuthMethods);
  }, []);

  // SSO-only: no local Google or password, redirect to control plane
  useEffect(() => {
    if (authMethods && authMethods.sso && !authMethods.google && !authMethods.password && authMethods.sso_url) {
      setSsoRedirecting(true);
      window.location.href = authMethods.sso_url;
    }
  }, [authMethods]);

  if (ssoRedirecting) {
    return (
      <div style={{
        position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
        background: '#030305', display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: 'rgba(255, 255, 255, 0.7)', fontSize: '1rem', zIndex: 1000,
      }}>
        Redirecting to sign in...
      </div>
    );
  }

  const handleLoginSuccess = () => {
    // The AuthProvider will handle updating the authentication state
  };

  const handleLoginError = (error: string) => {
    toast.error(error);
  };

  const handlePasswordSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password.trim()) return;
    setIsPasswordLoading(true);
    setPasswordError(null);
    try {
      const result = await loginWithPassword(password);
      if (result.ok) {
        window.location.reload();
      } else {
        setPasswordError(result.error || 'Invalid password');
      }
    } catch {
      setPasswordError('Login failed. Please try again.');
    } finally {
      setIsPasswordLoading(false);
    }
  };

  const handleDevLogin = async () => {
    setIsDevLoginLoading(true);
    try {
      await loginWithDevAccount();
      window.location.reload();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Dev login failed');
    } finally {
      setIsDevLoginLoading(false);
    }
  };

  const showGoogle = authMethods?.google ?? false;
  const showPassword = authMethods?.password ?? false;

  return (
    <div
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: '#030305',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
      }}
    >
      <div
        style={{
          background: 'rgba(255, 255, 255, 0.03)',
          border: '1px solid rgba(255, 255, 255, 0.08)',
          backdropFilter: 'blur(16px)',
          padding: '2.5rem',
          borderRadius: '16px',
          textAlign: 'center',
          minWidth: '340px',
          maxWidth: '400px',
        }}
      >
        <h2 style={{ marginBottom: '1.5rem', color: 'rgba(255, 255, 255, 0.95)', fontSize: '20px', fontWeight: 600 }}>
          Sign in to Longhouse
        </h2>

        {!authMethods && (
          <div style={{ color: 'rgba(255, 255, 255, 0.5)', padding: '1rem 0' }}>Loading...</div>
        )}

        {showGoogle && (
          <GoogleSignInButton
            clientId={clientId}
            onSuccess={handleLoginSuccess}
            onError={handleLoginError}
          />
        )}

        {showPassword && showGoogle && (
          <div style={{ margin: '1rem 0', color: 'rgba(255, 255, 255, 0.3)', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <div style={{ flex: 1, height: '1px', background: 'rgba(255, 255, 255, 0.1)' }} />
            <span>or</span>
            <div style={{ flex: 1, height: '1px', background: 'rgba(255, 255, 255, 0.1)' }} />
          </div>
        )}

        {showPassword && (
          <form onSubmit={handlePasswordSubmit}>
            <input
              type="password"
              value={password}
              onChange={(e) => { setPassword(e.target.value); setPasswordError(null); }}
              placeholder="Enter password"
              autoFocus={!showGoogle}
              style={{
                width: '100%',
                padding: '0.75rem 1rem',
                background: 'rgba(255, 255, 255, 0.07)',
                border: `1px solid ${passwordError ? '#ef4444' : 'rgba(255, 255, 255, 0.12)'}`,
                borderRadius: '8px',
                fontSize: '14px',
                color: 'rgba(255, 255, 255, 0.9)',
                boxSizing: 'border-box',
                marginBottom: '0.5rem',
                outline: 'none',
              }}
            />
            {passwordError && (
              <div style={{ color: '#ef4444', fontSize: '13px', marginBottom: '0.5rem', textAlign: 'left' }}>
                {passwordError}
              </div>
            )}
            <button
              type="submit"
              disabled={isPasswordLoading || !password.trim()}
              style={{
                width: '100%',
                padding: '0.75rem',
                background: 'linear-gradient(135deg, #06b6d4 0%, #22d3ee 100%)',
                color: '#030305',
                border: 'none',
                borderRadius: '8px',
                fontSize: '14px',
                fontWeight: 600,
                cursor: isPasswordLoading || !password.trim() ? 'not-allowed' : 'pointer',
                opacity: isPasswordLoading || !password.trim() ? 0.5 : 1,
                marginTop: '0.25rem',
              }}
            >
              {isPasswordLoading ? 'Signing in...' : 'Sign In'}
            </button>
          </form>
        )}

        {config.isDevelopment && (
          <>
            <div style={{ margin: '1rem 0', color: 'rgba(255, 255, 255, 0.3)' }}>or</div>
            <button
              onClick={handleDevLogin}
              disabled={isDevLoginLoading}
              style={{
                padding: '0.75rem 2rem',
                background: 'rgba(16, 185, 129, 0.2)',
                color: '#10b981',
                border: '1px solid rgba(16, 185, 129, 0.3)',
                borderRadius: '8px',
                fontSize: '14px',
                fontWeight: 600,
                cursor: isDevLoginLoading ? 'not-allowed' : 'pointer',
                opacity: isDevLoginLoading ? 0.5 : 1,
              }}
            >
              {isDevLoginLoading ? 'Logging in...' : 'Dev Login (Local Only)'}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

// Auth guard component
interface AuthGuardProps {
  children: ReactNode;
  clientId: string;
}

export function AuthGuard({ children, clientId }: AuthGuardProps) {
  const { isAuthenticated, isLoading } = useAuth();
  const { status: serviceStatus, retryCount, retry } = useServiceHealth();

  // Skip auth guard if authentication is not real (dev/demo modes)
  if (!config.authEnabled || config.demoMode) {
    return <>{children}</>;
  }

  // Show service unavailable screen when backend is not reachable
  // This happens during deployments, restarts, or network issues
  if (serviceStatus === 'unavailable' || serviceStatus === 'checking') {
    // During initial check, show unavailable if we've already failed once
    // This prevents flash of "Loading..." followed by unavailable
    if (serviceStatus === 'checking' && retryCount === 0) {
      return (
        <div style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100vh',
          fontSize: '1.2rem',
          background: 'linear-gradient(135deg, #1a1a2e 0%, #16213e 100%)',
          color: 'rgba(255, 255, 255, 0.7)',
        }}>
          Loading...
        </div>
      );
    }
    return <ServiceUnavailable retryCount={retryCount} onRetry={retry} />;
  }

  // Service is available - now check authentication
  if (isLoading) {
    return (
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100vh',
        fontSize: '1.2rem',
        background: 'linear-gradient(135deg, #1a1a2e 0%, #16213e 100%)',
        color: 'rgba(255, 255, 255, 0.7)',
      }}>
        Loading...
      </div>
    );
  }

  if (!isAuthenticated) {
    return <LoginOverlay clientId={clientId} />;
  }

  return <>{children}</>;
}
