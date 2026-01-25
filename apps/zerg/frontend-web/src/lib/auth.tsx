import React, { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'react-hot-toast';
import config from './config';

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
  is_demo?: boolean;
}

interface AuthContextType {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (idToken: string) => Promise<void>;
  logout: () => void;
  getToken: () => string | null;
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
    throw new Error(error || 'Login failed');
  }

  return response.json();
}

async function getCurrentUser(): Promise<User> {
  const response = await fetch(`${config.apiBaseUrl}/users/me`, {
    credentials: 'include', // Use cookie for auth
  });

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error('Authentication expired');
    }
    throw new Error('Failed to get user profile');
  }

  return response.json();
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
  const [user, setUser] = useState<User | null>(null);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const queryClient = useQueryClient();

  // Clean up any legacy localStorage token on mount
  useEffect(() => {
    cleanupLegacyToken();
  }, []);

  // Check auth status via cookie on mount (always enabled - cookie determines auth)
  const { data: userData, isLoading, error, refetch } = useQuery<User>({
    queryKey: ['current-user'],
    queryFn: getCurrentUser,
    enabled: true, // Always try - cookie auth is checked server-side
    retry: false,
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
    } else if (error) {
      setUser(null);
      setIsAuthenticated(false);
    }
  }, [userData, error]);

  const login = async (idToken: string) => {
    await loginMutation.mutateAsync(idToken);
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

  const value: AuthContextType = {
    user,
    isAuthenticated,
    isLoading,
    login,
    logout,
    getToken,
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
  const [isDevLoginLoading, setIsDevLoginLoading] = useState(false);

  const handleLoginSuccess = () => {
    // The AuthProvider will handle updating the authentication state
  };

  const handleLoginError = (error: string) => {
    toast.error(error);
  };

  const handleDevLogin = async () => {
    setIsDevLoginLoading(true);
    try {
      await loginWithDevAccount();
      // Cookie is set by server; reload to trigger auth state update
      window.location.reload();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Dev login failed');
    } finally {
      setIsDevLoginLoading(false);
    }
  };

  return (
    <div
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: 'rgba(255, 255, 255, 0.1)',
        backdropFilter: 'blur(4px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
      }}
    >
      <div
        style={{
          background: 'white',
          padding: '2rem',
          borderRadius: '8px',
          boxShadow: '0 4px 20px rgba(0, 0, 0, 0.1)',
          textAlign: 'center',
        }}
      >
        <h2 style={{ marginBottom: '1rem', color: '#333' }}>Sign in to Zerg</h2>
        <GoogleSignInButton
          clientId={clientId}
          onSuccess={handleLoginSuccess}
          onError={handleLoginError}
        />
        {config.isDevelopment && (
          <>
            <div style={{ margin: '1rem 0', color: '#666' }}>or</div>
            <button
              onClick={handleDevLogin}
              disabled={isDevLoginLoading}
              style={{
                padding: '0.75rem 2rem',
                background: '#10b981',
                color: 'white',
                border: 'none',
                borderRadius: '4px',
                fontSize: '14px',
                fontWeight: 600,
                cursor: isDevLoginLoading ? 'not-allowed' : 'pointer',
                opacity: isDevLoginLoading ? 0.6 : 1,
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

  // Skip auth guard if authentication is disabled (for demos/tests)
  if (!config.authEnabled) {
    return <>{children}</>;
  }

  if (isLoading) {
    return (
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100vh',
        fontSize: '1.2rem',
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
