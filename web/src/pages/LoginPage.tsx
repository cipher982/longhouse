import React, { useEffect, useRef, useState } from 'react';
import { Navigate, useSearchParams } from 'react-router-dom';
import { clearLogoutIntent, hasLogoutIntent, useAuth, useAuthMethods } from '../lib/auth';
import { clearLogoutBarrier } from '../lib/auth-refresh';
import { sanitizeReturnTo } from '../lib/loginRedirect';
import config from '../lib/config';

export default function LoginPage() {
  const [params] = useSearchParams();
  const returnTo = sanitizeReturnTo(params.get('return_to'));
  const authError = params.get('auth_error');
  const [logoutSuppressed, setLogoutSuppressed] = useState(hasLogoutIntent);
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
    cp_unavailable: 'The account service is temporarily unavailable. Try again in a moment.',
    catalog_unavailable: 'The instance is temporarily unavailable. Try again in a moment.',
    auth_misconfigured: 'This instance cannot complete secure sign-in right now. Try again later.',
    cookie_loop: 'This browser could not keep the sign-in cookie. Check that cookies are enabled, then try again.',
    handoff_failed: 'Longhouse could not finish signing you in. Start again.',
  };
  const errorMessage = authError
    ? errorMessages[authError] ?? 'Longhouse could not finish signing you in. Start again.'
    : null;

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
    navigationStarted.current = true;
    clearLogoutIntent();
    clearLogoutBarrier();
    setLogoutSuppressed(false);
    window.location.assign(retryUrl);
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        background: '#120B09',
        display: 'flex',
        alignItems: 'center',
        color: 'rgba(243, 234, 217, 0.7)',
        fontSize: '1rem',
        textAlign: 'center',
        padding: '2rem',
      }}
    >
      <div>
        {errorMessage ? (
          <>
            <p role="alert">{errorMessage}</p>
            <button type="button" onClick={beginLogin}>
              Try signing in again
            </button>
          </>
        ) : logoutSuppressed ? (
          <>
            <p role="status">You are signed out.</p>
            <button type="button" onClick={beginLogin}>
              Sign in again
            </button>
          </>
        ) : authUnavailable ? (
          <>
            <p role="alert">Authentication is temporarily unavailable.</p>
            <button type="button" onClick={() => void refreshAuth()}>
              Try again
            </button>
          </>
        ) : methodsLoading ? (
          'Loading…'
        ) : methodsError ? (
          <>
            <p role="alert">Longhouse is temporarily unavailable. Try again.</p>
            <button type="button" onClick={() => void refetchAuthMethods()}>
              Try again
            </button>
          </>
        ) : authMethods?.sso ? (
          'Taking you to your Longhouse account…'
        ) : (
          'Sign-in is not configured for this instance.'
        )}
      </div>
    </div>
  );
}

// Named export so AuthGuard can pass clientId — kept for backward compat during transition
export { LoginPage };
