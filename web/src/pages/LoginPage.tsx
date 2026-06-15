import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { sanitizeReturnTo } from '../lib/loginRedirect';
import { useAuthMethods } from '../lib/auth';

export default function LoginPage() {
  const [params] = useSearchParams();
  const returnTo = sanitizeReturnTo(params.get('return_to'));
  const { data: authMethods, isLoading: methodsLoading } = useAuthMethods();
  const [navigated, setNavigated] = useState(false);

  useEffect(() => {
    if (navigated || methodsLoading || !authMethods) return;

    if (authMethods.sso) {
      // Hosted tenant: server route sets nothing and 302s to CP /auth/start.
      setNavigated(true);
      window.location.replace(
        `/api/auth/start-handoff?return_to=${encodeURIComponent(returnTo)}`,
      );
    }
    // For self-host, the React shell renders the legacy login form
    // (Google + password). Don't navigate anywhere; the user
    // authenticates locally. This avoids a self-host redirect loop.
  }, [authMethods, methodsLoading, navigated, returnTo]);

  return (
    <div
      style={{
        minHeight: '100vh',
        background: '#120B09',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: 'rgba(243, 234, 217, 0.7)',
        fontSize: '1rem',
      }}
    >
      {authMethods?.sso
        ? 'Taking you to your Longhouse account…'
        : 'Loading…'}
    </div>
  );
}

// Named export so AuthGuard can pass clientId — kept for backward compat during transition
export { LoginPage };
