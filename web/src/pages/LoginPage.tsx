import React, { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { toast } from 'react-hot-toast';
import { useAuth, useAuthMethods } from '../lib/auth';
import { sanitizeReturnTo } from '../lib/loginRedirect';
import config from '../lib/config';

// ---------------------------------------------------------------------------
// Internal helpers (mirrors logic previously in auth.tsx LoginOverlay)
// ---------------------------------------------------------------------------

function buildHostedLoginRedirectUrl(
  baseUrl: string | null | undefined,
  returnTo: string,
): string | null {
  if (!baseUrl) return null;
  try {
    const url = new URL(baseUrl);
    if (returnTo.startsWith('/')) {
      url.searchParams.set('return_to', returnTo);
    }
    return url.toString();
  } catch {
    return baseUrl;
  }
}

async function loginWithPassword(password: string): Promise<{ ok: boolean; error?: string }> {
  const response = await fetch(`${config.apiBaseUrl}/auth/password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ password }),
  });
  if (response.ok) return { ok: true };
  if (response.status === 400) return { ok: false, error: 'Password auth not configured' };
  if (response.status === 429) {
    const retryAfter = response.headers.get('Retry-After');
    const suffix = retryAfter ? ` Try again in ${retryAfter}s.` : ' Try again later.';
    return { ok: false, error: `Too many attempts.${suffix}` };
  }
  return { ok: false, error: 'Invalid password' };
}

async function loginWithDevAccount(): Promise<void> {
  const response = await fetch(`${config.apiBaseUrl}/auth/dev-login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || 'Dev login failed');
  }
}

// ---------------------------------------------------------------------------
// Google Sign-In button (inline — avoids circular dep with auth.tsx)
// ---------------------------------------------------------------------------

interface GoogleButtonProps {
  clientId: string;
  onToken: (idToken: string) => void;
  onError: (msg: string) => void;
}

function GoogleSignInButton({ clientId, onToken, onError }: GoogleButtonProps) {
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    const script = document.createElement('script');
    script.src = 'https://accounts.google.com/gsi/client';
    script.async = true;
    script.defer = true;
    document.head.appendChild(script);

    script.onload = () => {
      if (window.google?.accounts?.id) {
        window.google.accounts.id.initialize({
          client_id: clientId,
          callback: async (response: { credential: string }) => {
            setIsLoading(true);
            try {
              onToken(response.credential);
            } catch (error) {
              onError(error instanceof Error ? error.message : 'Login failed');
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

    return () => { script.remove(); };
  }, [clientId, onToken, onError]);

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
      <div id="google-signin-button" />
      {isLoading && <div style={{ color: 'rgba(243,234,217,0.6)', fontSize: '13px' }}>Signing in...</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// LoginPage
// ---------------------------------------------------------------------------

export default function LoginPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { isAuthenticated, isLoading: authLoading, login } = useAuth();
  const { data: authMethods } = useAuthMethods();

  const returnTo = sanitizeReturnTo(searchParams.get('return_to'));

  const [password, setPassword] = useState('');
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [isPasswordLoading, setIsPasswordLoading] = useState(false);
  const [isDevLoginLoading, setIsDevLoginLoading] = useState(false);
  const [ssoRedirecting, setSsoRedirecting] = useState(false);

  // Already authenticated — go to return_to destination
  useEffect(() => {
    if (!authLoading && isAuthenticated) {
      navigate(returnTo, { replace: true });
    }
  }, [authLoading, isAuthenticated, navigate, returnTo]);

  // SSO-only: redirect to control plane immediately
  useEffect(() => {
    const hostedLoginUrl = buildHostedLoginRedirectUrl(
      authMethods?.sso_login_url || authMethods?.sso_url,
      returnTo,
    );
    if (authMethods && authMethods.sso && !authMethods.google && !authMethods.password && hostedLoginUrl) {
      setSsoRedirecting(true);
      window.location.href = hostedLoginUrl;
    }
  }, [authMethods, returnTo]);

  const finishLogin = () => {
    navigate(returnTo, { replace: true });
  };

  const handleGoogleToken = async (idToken: string) => {
    try {
      await login(idToken);
      finishLogin();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Login failed');
    }
  };

  const handlePasswordSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password.trim()) return;
    setIsPasswordLoading(true);
    setPasswordError(null);
    try {
      const result = await loginWithPassword(password);
      if (result.ok) {
        finishLogin();
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
      finishLogin();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Dev login failed');
    } finally {
      setIsDevLoginLoading(false);
    }
  };

  if (ssoRedirecting || (authLoading && !authMethods)) {
    return (
      <div style={{
        position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
        background: '#120B09', display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: 'rgba(243, 234, 217, 0.7)', fontSize: '1rem',
      }}>
        {ssoRedirecting ? 'Redirecting to sign in...' : 'Loading...'}
      </div>
    );
  }

  const showGoogle = authMethods?.google ?? false;
  const showPassword = authMethods?.password ?? false;
  const showSso = !!(authMethods?.sso && authMethods?.sso_url);
  const hostedLoginUrl = buildHostedLoginRedirectUrl(
    authMethods?.sso_login_url || authMethods?.sso_url,
    returnTo,
  );
  const ssoBase = authMethods?.sso_url ? authMethods.sso_url.replace(/\/+$/, '') : null;
  const ssoHost = (() => {
    if (!ssoBase) return null;
    try { return new URL(ssoBase).host; } catch { return null; }
  })();
  const switchAccountUrl = ssoBase
    ? `${ssoBase}/auth/logout?return_to=${encodeURIComponent(`${ssoBase}/?switch=1`)}`
    : null;

  return (
    <div style={{
      minHeight: '100vh',
      background: '#120B09',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
    }}>
      <div style={{
        background: '#1A1410',
        border: '1px solid #231E16',
        padding: '2.5rem',
        borderRadius: '16px',
        textAlign: 'center',
        minWidth: '340px',
        maxWidth: '400px',
        width: '100%',
      }}>
        <h2 style={{ marginBottom: '1.5rem', color: 'rgba(243, 234, 217, 0.95)', fontSize: '20px', fontWeight: 600 }}>
          Sign in to Longhouse
        </h2>

        {!authMethods && (
          <div style={{ color: 'rgba(181, 164, 142, 0.5)', padding: '1rem 0' }}>Loading...</div>
        )}

        {showSso && (
          <>
            <button
              onClick={() => { window.location.href = hostedLoginUrl!; }}
              style={{
                width: '100%', padding: '0.75rem',
                background: 'linear-gradient(135deg, #C9A66B 0%, #D4B87A 100%)',
                color: '#120B09', border: 'none', borderRadius: '8px',
                fontSize: '14px', fontWeight: 600, cursor: 'pointer',
              }}
            >
              Continue to your Longhouse account
            </button>
            <div style={{ marginTop: '0.6rem', color: 'rgba(181, 164, 142, 0.55)', fontSize: '12px' }}>
              Redirects to {ssoHost ?? 'control.longhouse.ai'} to sign in
            </div>
            {switchAccountUrl && (
              <button
                type="button"
                onClick={() => { window.location.href = switchAccountUrl; }}
                style={{
                  marginTop: '0.5rem', background: 'transparent', border: 'none',
                  color: 'rgba(243, 234, 217, 0.7)', fontSize: '12px',
                  cursor: 'pointer', textDecoration: 'underline',
                }}
              >
                Switch account
              </button>
            )}
          </>
        )}

        {showGoogle && !showSso && (
          <GoogleSignInButton
            clientId={config.googleClientId}
            onToken={handleGoogleToken}
            onError={(msg) => toast.error(msg)}
          />
        )}

        {showPassword && (showGoogle || showSso) && (
          <div style={{ margin: '1rem 0', color: 'rgba(181, 164, 142, 0.3)', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <div style={{ flex: 1, height: '1px', background: 'rgba(243, 234, 217, 0.1)' }} />
            <span>or</span>
            <div style={{ flex: 1, height: '1px', background: 'rgba(243, 234, 217, 0.1)' }} />
          </div>
        )}

        {showPassword && (
          <form onSubmit={handlePasswordSubmit}>
            <input
              type="password"
              value={password}
              onChange={(e) => { setPassword(e.target.value); setPasswordError(null); }}
              placeholder="Enter password"
              autoFocus={!showGoogle && !showSso}
              style={{
                width: '100%', padding: '0.75rem 1rem', background: '#211C15',
                border: `1px solid ${passwordError ? '#C45040' : '#2a2418'}`,
                borderRadius: '8px', fontSize: '14px',
                color: 'rgba(243, 234, 217, 0.9)', boxSizing: 'border-box',
                marginBottom: '0.5rem', outline: 'none',
              }}
            />
            {passwordError && (
              <div style={{ color: '#C45040', fontSize: '13px', marginBottom: '0.5rem', textAlign: 'left' }}>
                {passwordError}
              </div>
            )}
            <button
              type="submit"
              disabled={isPasswordLoading || !password.trim()}
              style={{
                width: '100%', padding: '0.75rem',
                background: 'linear-gradient(135deg, #C9A66B 0%, #D4B87A 100%)',
                color: '#120B09', border: 'none', borderRadius: '8px',
                fontSize: '14px', fontWeight: 600,
                cursor: isPasswordLoading || !password.trim() ? 'not-allowed' : 'pointer',
                opacity: isPasswordLoading || !password.trim() ? 0.5 : 1,
                marginTop: '0.25rem',
              }}
            >
              {isPasswordLoading ? 'Signing in...' : 'Sign In'}
            </button>
          </form>
        )}

        {!showGoogle && !showPassword && !showSso && authMethods && (
          <div style={{ color: 'rgba(181, 164, 142, 0.5)', fontSize: '13px', padding: '0.5rem 0' }}>
            This Longhouse server does not advertise a supported sign-in method.
          </div>
        )}

        {config.isDevelopment && (
          <>
            <div style={{ margin: '1rem 0', color: 'rgba(181, 164, 142, 0.3)' }}>or</div>
            <button
              onClick={handleDevLogin}
              disabled={isDevLoginLoading}
              style={{
                padding: '0.75rem 2rem',
                background: 'rgba(93, 155, 74, 0.2)', color: '#5D9B4A',
                border: '1px solid rgba(93, 155, 74, 0.3)', borderRadius: '8px',
                fontSize: '14px', fontWeight: 600,
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

// Named export so AuthGuard can pass clientId — kept for backward compat during transition
export { LoginPage };
