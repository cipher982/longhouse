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

// JWT token storage
const TOKEN_STORAGE_KEY = 'zerg_jwt';

function getStoredToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

function setStoredToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    // Ignore storage errors
  }
}

function removeStoredToken(): void {
  try {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // Ignore storage errors
  }
}

// API functions
async function loginWithGoogle(idToken: string): Promise<{ access_token: string; expires_in: number }> {
  const response = await fetch(`${config.apiBaseUrl}/auth/google`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ id_token: idToken }),
  });

  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || 'Login failed');
  }

  return response.json();
}

async function getCurrentUser(): Promise<User> {
  const token = getStoredToken();
  if (!token) {
    throw new Error('No auth token');
  }

  const response = await fetch(`${config.apiBaseUrl}/users/me`, {
    headers: {
      'Authorization': `Bearer ${token}`,
    },
  });

  if (!response.ok) {
    if (response.status === 401) {
      removeStoredToken();
      throw new Error('Authentication expired');
    }
    throw new Error('Failed to get user profile');
  }

  return response.json();
}

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [user, setUser] = useState<User | null>(null);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const queryClient = useQueryClient();

  // Check if we have a stored token on mount
  const { data: userData, isLoading, error, refetch } = useQuery<User>({
    queryKey: ['current-user'],
    queryFn: getCurrentUser,
    enabled: !!getStoredToken(),
    retry: false,
    staleTime: 5 * 60 * 1000, // 5 minutes
  });

  const loginMutation = useMutation({
    mutationFn: loginWithGoogle,
    onSuccess: (data) => {
      setStoredToken(data.access_token);
      // Refetch user data after successful login
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
      removeStoredToken();
    }
  }, [userData, error]);

  const login = async (idToken: string) => {
    await loginMutation.mutateAsync(idToken);
  };

  const logout = () => {
    removeStoredToken();
    setUser(null);
    setIsAuthenticated(false);
    queryClient.clear();
  };

  const getToken = () => {
    return getStoredToken();
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
  const { login } = useAuth();

  const handleLoginSuccess = () => {
    // The AuthProvider will handle updating the authentication state
  };

  const handleLoginError = (error: string) => {
    toast.error(error);
  };

  const handleDevLogin = async () => {
    setIsDevLoginLoading(true);
    try {
      const data = await loginWithDevAccount();
      setStoredToken(data.access_token);
      window.location.reload(); // Reload to trigger auth state update
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