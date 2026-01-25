/**
 * Cross-Origin Authentication Tests - Core Suite
 *
 * These tests verify cookie-based authentication works across different domains
 * (split deployment: swarmlet.com for frontend, api.swarmlet.com for backend).
 *
 * REQUIRES: SMOKE_TEST_SECRET environment variable set
 *
 * Cookie requirements for split deployment:
 * - SameSite=None: Required for cross-origin requests
 * - Secure: Required when SameSite=None
 * - Domain=.swarmlet.com: Optional, enables subdomain sharing
 *
 * CORE SUITE: 0 skipped, 0 flaky, retries: 0
 */

import { test, expect } from '../fixtures';

// These tests only run when SMOKE_TEST_SECRET is configured
// Skip entire describe block if not in production smoke test mode
const shouldRun = !!process.env.SMOKE_TEST_SECRET;

test.describe('Cross-Origin Authentication - Core', () => {
  test.skip(!shouldRun, 'SMOKE_TEST_SECRET required for cross-origin tests');

  test('service login sets cookie with correct attributes', async ({ request }) => {
    const apiUrl = process.env.API_URL || process.env.PLAYWRIGHT_BACKEND_URL || '';
    test.skip(!apiUrl, 'API_URL required');

    // 1. Login via service account
    const loginRes = await request.post(`${apiUrl}/api/auth/service-login`, {
      headers: {
        'X-Service-Secret': process.env.SMOKE_TEST_SECRET!,
      },
    });
    expect(loginRes.status()).toBe(200);

    // 2. Verify cookie attributes (per Codex review)
    const setCookieHeader = loginRes.headers()['set-cookie'];
    expect(setCookieHeader).toBeDefined();

    // Parse the Set-Cookie header
    const cookieString = setCookieHeader?.toLowerCase() || '';

    // For cross-origin to work, cookie needs:
    // - Session cookie present
    expect(cookieString).toMatch(/swarmlet_session=/);

    // - SameSite=None (allows cross-origin)
    expect(cookieString).toContain('samesite=none');

    // - Secure (required with SameSite=None)
    expect(cookieString).toContain('secure');

    // 3. Verify login response contains user info
    const loginData = await loginRes.json();
    expect(loginData).toHaveProperty('id');
    expect(loginData).toHaveProperty('email');
  });

  test('authenticated request with cookie succeeds', async ({ request }) => {
    const apiUrl = process.env.API_URL || process.env.PLAYWRIGHT_BACKEND_URL || '';
    test.skip(!apiUrl, 'API_URL required');

    // 1. Login first
    const loginRes = await request.post(`${apiUrl}/api/auth/service-login`, {
      headers: {
        'X-Service-Secret': process.env.SMOKE_TEST_SECRET!,
      },
    });
    expect(loginRes.status()).toBe(200);

    // 2. Make authenticated request (cookie should be sent automatically)
    const verifyRes = await request.get(`${apiUrl}/api/auth/verify`);
    expect(verifyRes.status()).toBe(204);
  });

  test('unauthenticated request returns 401', async ({ request }) => {
    const apiUrl = process.env.API_URL || process.env.PLAYWRIGHT_BACKEND_URL || '';
    test.skip(!apiUrl, 'API_URL required');

    // Create new request context without cookies
    const response = await request.get(`${apiUrl}/api/auth/verify`, {
      headers: {
        // Explicitly exclude auth headers
        'Authorization': '',
      },
    });

    // In production (AUTH_DISABLED=false), should return 401
    // In dev (AUTH_DISABLED=true), returns 204
    const isDevMode = process.env.AUTH_DISABLED === '1' || process.env.AUTH_DISABLED === 'true';
    if (isDevMode) {
      expect(response.status()).toBe(204);
    } else {
      expect(response.status()).toBe(401);
    }
  });

  test('browser credential flow works for API requests', async ({ page, context }) => {
    const apiUrl = process.env.API_URL || process.env.PLAYWRIGHT_BACKEND_URL || '';
    const frontendUrl = process.env.FRONTEND_URL || process.env.PLAYWRIGHT_FRONTEND_BASE || '';
    test.skip(!apiUrl || !frontendUrl, 'API_URL and FRONTEND_URL required');

    // 1. Login via API to get session cookie
    const loginRes = await page.request.post(`${apiUrl}/api/auth/service-login`, {
      headers: {
        'X-Service-Secret': process.env.SMOKE_TEST_SECRET!,
      },
    });
    expect(loginRes.status()).toBe(200);

    // 2. Get cookies and verify they're set for the right domain
    const cookies = await context.cookies();
    const sessionCookie = cookies.find((c) => c.name === 'swarmlet_session');

    // Note: In local E2E tests (same origin), cookie may not have cross-origin attributes
    // This test is primarily for production smoke testing
    if (process.env.FRONTEND_URL && process.env.API_URL) {
      expect(sessionCookie).toBeDefined();
    }

    // 3. Navigate to frontend
    await page.goto(frontendUrl);

    // 4. Wait for app to load
    await page.waitForLoadState('networkidle');

    // 5. Check that the auth state is detected
    // In the UI, authenticated users should see the dashboard or chat
    const isAuthenticated = await page.evaluate(() => {
      // Check for signs of authenticated state
      // This could be a user menu, dashboard content, etc.
      return (
        document.querySelector('[data-testid="user-menu"]') !== null ||
        document.querySelector('[data-testid="create-agent-btn"]') !== null ||
        window.location.pathname.includes('/dashboard') ||
        window.location.pathname.includes('/chat')
      );
    });

    // In dev mode, auth is bypassed anyway
    const isDevMode = process.env.AUTH_DISABLED === '1' || process.env.AUTH_DISABLED === 'true';
    if (!isDevMode) {
      expect(isAuthenticated).toBe(true);
    }
  });
});
