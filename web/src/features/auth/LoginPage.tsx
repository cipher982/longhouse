import React, { useEffect, useRef, useState } from 'react';
import { Navigate, useSearchParams } from 'react-router';
import { clearLogoutBarrier, markLoginAttempt } from './auth-refresh';
import { sanitizeReturnTo } from './loginRedirect';
import config from '@/shared/lib/config';
import { clearLogoutIntent, hasLogoutIntent, useAuth, useAuthMethods } from './auth';
import { Button, Input } from '@/shared/ui';
import { useReadinessFlag } from '@/shared/lib/readiness-contract';
import { SwarmLogo } from '@/shared/ui/SwarmLogo';
import './login.css';

export default function LoginPage() {
  const [params] = useSearchParams();
  const returnTo = sanitizeReturnTo(params.get('return_to'));
  const authError = params.get('auth_error');
  const [logoutSuppressed, setLogoutSuppressed] = useState(hasLogoutIntent);
  const [password, setPassword] = useState('');
  const [passwordSubmitting, setPasswordSubmitting] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const googleButtonRef = useRef<HTMLDivElement>(null);
  const loginRef = useRef<(idToken: string) => Promise<unknown>>(async () => undefined);
  const navigationStarted = useRef(false);
  const {
    data: authMethods,
    isLoading: methodsLoading,
    isError: methodsError,
    refetch: refetchAuthMethods,
  } = useAuthMethods();
  const {
    user: authenticatedUser,
    isLoading: authLoading,
    authUnavailable,
    login,
    loginPassword,
    refreshAuth,
  } = useAuth();

  const errorMessages: Record<string, string> = {
    login_state_not_returned: 'Your sign-in response was missing its browser binding. Start again.',
    login_state_malformed: 'Your sign-in response was invalid. Start again.',
    login_cookie_absent: 'Your sign-in browser cookie was unavailable. Check cookies and start again.',
    login_cookie_mismatch: 'Your sign-in browser cookie did not match. Start again.',
    login_state_missing: 'Your sign-in attempt expired or was opened in a different browser tab.',
    login_state_mismatch: 'Your sign-in attempt could not be verified. Start again.',
    handoff_expired: 'That sign-in link expired. Start again.',
    rate_limited: 'There were too many sign-in attempts. Wait a moment and try again.',
    cp_unavailable: 'The account service is temporarily unavailable. Try again in a moment.',
    catalog_unavailable: 'The instance is temporarily unavailable. Try again in a moment.',
    auth_misconfigured: 'This instance cannot complete secure sign-in right now. Try again later.',
    cookie_loop: 'This browser could not keep the sign-in cookie. Check that cookies are enabled, then try again.',
    handoff_failed: 'Longhouse could not finish signing you in. Start again.',
  };
  const errorMessage = authError
    ? errorMessages[authError] ?? 'Longhouse could not finish signing you in. Start again.'
    : null;

  loginRef.current = login;
  useReadinessFlag({ ready: !methodsLoading && !authLoading });

  useEffect(() => {
    if (!authMethods?.google || !config.googleClientId || !googleButtonRef.current) return;
    const renderGoogleButton = () => {
      const google = window.google;
      if (!google?.accounts?.id || !googleButtonRef.current) return;
      google.accounts.id.initialize({
        client_id: config.googleClientId,
        callback: (result) => {
          if (!result.credential) return;
          void loginRef.current(result.credential).catch(() => {
            // The auth mutation surfaces the provider error through the toast.
          });
        },
      });
      google.accounts.id.renderButton(googleButtonRef.current, {
        theme: 'outline',
        size: 'large',
      });
    };

    let script = document.querySelector<HTMLScriptElement>('script[data-longhouse-google-identity]');
    if (!script) {
      script = document.createElement('script');
      script.src = 'https://accounts.google.com/gsi/client';
      script.async = true;
      script.defer = true;
      script.dataset.longhouseGoogleIdentity = '1';
      document.head.appendChild(script);
    }
    if (window.google) renderGoogleButton();
    else script.addEventListener('load', renderGoogleButton, { once: true });
    return () => script?.removeEventListener('load', renderGoogleButton);
  }, [authMethods?.google]);

  useEffect(() => {
    if (
      authError ||
      logoutSuppressed ||
      methodsLoading ||
      !authMethods ||
      authLoading ||
      authUnavailable ||
      authenticatedUser ||
      !authMethods.sso ||
      navigationStarted.current
    ) {
      return;
    }

    // Hosted tenant: the tenant route owns the state cookie and redirects to
    // the CP. Keep this effect single-owner so a React rerender cannot issue a
    // second handoff while the browser is still following the first one.
    markLoginAttempt();
    navigationStarted.current = true;
    window.location.replace(
      `/api/auth/start-handoff?return_to=${encodeURIComponent(returnTo)}`,
    );
  }, [
    authError,
    authLoading,
    authMethods,
    authUnavailable,
    authenticatedUser,
    methodsLoading,
    returnTo,
    logoutSuppressed,
  ]);
  const submitPassword = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPasswordError(null);
    setPasswordSubmitting(true);
    try {
      await loginPassword(password);
      setPassword('');
      await refreshAuth();
    } catch (error) {
      setPasswordError(error instanceof Error ? error.message : 'Login failed. Try again.');
    } finally {
      setPasswordSubmitting(false);
    }
  };



  if (!config.authEnabled) {
    return <Navigate to="/timeline" replace />;
  }
  if (authenticatedUser) {
    return <Navigate to={returnTo || '/timeline'} replace />;
  }

  const retryUrl =
    `/api/auth/start-handoff?return_to=${encodeURIComponent(returnTo)}` +
    (authError === 'cookie_loop' ? '&reset_attempt=1' : '');
  const beginLogin = () => {
    clearLogoutIntent();
    clearLogoutBarrier();
    markLoginAttempt();
    navigationStarted.current = true;
    setLogoutSuppressed(false);
    window.location.assign(retryUrl);
  };

  return (
    <div className="login-page">
      <main className="login-card">
        <div className="login-brand">
          <SwarmLogo size={44} />
          <span className="login-wordmark">Longhouse</span>
        </div>
        {errorMessage ? (
          <>
            <p role="alert" className="login-message login-message--error">{errorMessage}</p>
            <Button variant="primary" onClick={beginLogin}>
              Try signing in again
            </Button>
          </>
        ) : logoutSuppressed ? (
          <>
            <p role="status" className="login-message">You are signed out.</p>
            <Button variant="primary" onClick={beginLogin}>
              Sign in again
            </Button>
          </>
        ) : authUnavailable ? (
          <>
            <p role="alert" className="login-message login-message--error">Authentication is temporarily unavailable.</p>
            <Button variant="primary" onClick={() => void refreshAuth()}>
              Try again
            </Button>
          </>
        ) : methodsLoading ? (
          <p className="login-message">Loading…</p>
        ) : methodsError ? (
          <>
            <p role="alert" className="login-message login-message--error">Longhouse is temporarily unavailable. Try again.</p>
            <Button variant="primary" onClick={() => void refetchAuthMethods()}>
              Try again
            </Button>
          </>
        ) : authMethods?.sso ? (
          <p className="login-message">Taking you to your Longhouse account…</p>
        ) : authMethods?.password || authMethods?.google ? (
          <>
            {authMethods.password && (
              <form onSubmit={submitPassword} className="login-form">
                <label htmlFor="longhouse-password" className="login-label">Instance password</label>
                <Input
                  id="longhouse-password"
                  type="password"
                  autoComplete="current-password"
                  autoFocus
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  disabled={passwordSubmitting}
                  required
                />
                <Button type="submit" variant="primary" disabled={passwordSubmitting}>
                  {passwordSubmitting ? 'Signing in…' : 'Sign in'}
                </Button>
                {passwordError ? <p role="alert" className="login-message login-message--error">{passwordError}</p> : null}
              </form>
            )}
            {authMethods.google && (
              <>
                {authMethods.password ? <p className="login-or">or</p> : null}
                <div ref={googleButtonRef} className="login-google" />
              </>
            )}
          </>
        ) : (
          <p className="login-message">Sign-in is not configured for this instance.</p>
        )}
      </main>
    </div>
  );
}

// Named export so AuthGuard can pass clientId — kept for backward compat during transition
export { LoginPage };
