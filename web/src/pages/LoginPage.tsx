import React, { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { toast } from 'react-hot-toast';
import { useAuth, useAuthMethods } from '../lib/auth';
import {
  extractTimelineSessionId,
  sanitizeReturnTo,
  shortSessionPrefix,
} from '../lib/loginRedirect';
import { loginWithPassword, loginWithDevAccount } from '../lib/authApi';
import { getProviderLabel } from '../lib/providers';
import { formatRelativeTime } from '../lib/sessionUtils';
import config from '../lib/config';

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

interface SessionPreview {
  session_id: string;
  provider: string;
  device_name: string | null;
  started_at: string;
  ended_at: string | null;
  owner_display_name: string | null;
  owner_email_local: string | null;
}

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

// ---------------------------------------------------------------------------
// Google Sign-In button (inline — avoids circular dep with auth.tsx)
// ---------------------------------------------------------------------------

interface GoogleButtonProps {
  clientId: string;
  onToken: (idToken: string) => Promise<void>;
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
          callback: (response: { credential: string }) => {
            setIsLoading(true);
            // onToken is async — run it as a detached task so the GSI callback
            // returns synchronously (GSI doesn't await the callback return value).
            void (async () => {
              try {
                await onToken(response.credential);
              } catch (error) {
                onError(error instanceof Error ? error.message : 'Login failed');
              } finally {
                setIsLoading(false);
              }
            })();
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
  const { isAuthenticated, isLoading: authLoading, login, refreshAuth } = useAuth();
  const { data: authMethods } = useAuthMethods();

  const returnTo = sanitizeReturnTo(searchParams.get('return_to'));

  const [password, setPassword] = useState('');
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [isPasswordLoading, setIsPasswordLoading] = useState(false);
  const [isDevLoginLoading, setIsDevLoginLoading] = useState(false);
  const [ssoRedirecting, setSsoRedirecting] = useState(false);
  const [sessionPreview, setSessionPreview] = useState<SessionPreview | null>(null);

  // If the visitor was bounced to /login from /timeline/<uuid>, surface whose
  // session they were trying to reach. Pure context — failure is silent so the
  // sign-in form still works when the session is gone or the network is flaky.
  useEffect(() => {
    // Drop any prior preview so a returnTo change (or a failed fetch) doesn't
    // leave stale attribution on screen for a different destination.
    setSessionPreview(null);
    if (authLoading || isAuthenticated) return;
    const sessionId = extractTimelineSessionId(returnTo);
    const prefix = sessionId ? shortSessionPrefix(sessionId) : null;
    if (!prefix) return;
    const controller = new AbortController();
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/s/${prefix}/preview`, { signal: controller.signal });
        if (!res.ok) return;
        const data = (await res.json()) as SessionPreview;
        // Only render if the resolved session matches the URL the visitor was
        // bounced from — a crafted return_to could otherwise surface
        // attribution for a different session than the one post-login
        // navigation will actually land on.
        if (!cancelled && sessionId && data.session_id === sessionId) {
          setSessionPreview(data);
        }
      } catch {
        // AbortError or network failure — render the login form without context.
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [authLoading, isAuthenticated, returnTo]);

  // Already authenticated — go to return_to destination
  useEffect(() => {
    if (!authLoading && isAuthenticated) {
      navigate(returnTo, { replace: true });
    }
  }, [authLoading, isAuthenticated, navigate, returnTo]);

  // SSO-only: redirect to control plane immediately (only when not already authenticated)
  useEffect(() => {
    if (authLoading || isAuthenticated) return;
    const hostedLoginUrl = buildHostedLoginRedirectUrl(
      authMethods?.sso_login_url || authMethods?.sso_url,
      returnTo,
    );
    if (authMethods && authMethods.sso && !authMethods.google && !authMethods.password && hostedLoginUrl) {
      setSsoRedirecting(true);
      window.location.href = hostedLoginUrl;
    }
  }, [authMethods, authLoading, isAuthenticated, returnTo]);

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
        await refreshAuth();
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
      await refreshAuth();
      finishLogin();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Dev login failed');
    } finally {
      setIsDevLoginLoading(false);
    }
  };

  // Hold the loading screen until we know auth state and have methods.
  // Prevents flash of login form for already-authenticated users, and prevents
  // SSO auto-redirect racing ahead of the authenticated redirect.
  if (ssoRedirecting || authLoading || isAuthenticated) {
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
        {sessionPreview && (
          <div
            data-testid="login-session-preview"
            style={{
              background: 'rgba(201, 166, 107, 0.08)',
              border: '1px solid rgba(201, 166, 107, 0.22)',
              borderRadius: '10px',
              padding: '0.75rem 1rem',
              marginBottom: '1.25rem',
              textAlign: 'left',
            }}
          >
            <div style={{
              fontSize: '10px',
              letterSpacing: '0.08em',
              textTransform: 'uppercase',
              color: 'rgba(201, 166, 107, 0.75)',
              marginBottom: '0.3rem',
              fontWeight: 600,
            }}>
              Trying to view
            </div>
            <div style={{
              color: 'rgba(243, 234, 217, 0.92)',
              fontSize: '14px',
              lineHeight: 1.4,
            }}>
              {sessionPreview.owner_display_name || sessionPreview.owner_email_local || 'Someone'}
              {"'s "}
              <span style={{ color: '#C9A66B', fontWeight: 600 }}>
                {getProviderLabel(sessionPreview.provider)}
              </span>
              {' session'}
              {sessionPreview.device_name ? ` on ${sessionPreview.device_name}` : ''}
              <span style={{ color: 'rgba(181, 164, 142, 0.65)', fontSize: '12px' }}>
                {sessionPreview.ended_at
                  ? ` · ended ${formatRelativeTime(sessionPreview.ended_at)}`
                  : ` · started ${formatRelativeTime(sessionPreview.started_at)}`}
              </span>
            </div>
          </div>
        )}

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
