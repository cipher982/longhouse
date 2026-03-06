/**
 * Hosted session authentication smoke - Live suite.
 *
 * Verifies the hosted login-token -> accept-token flow sets a usable session cookie
 * and that the browser carries that cookie to the hosted API.
 *
 * REQUIRES: SMOKE_LOGIN_TOKEN environment variable set.
 */

import { test, expect } from './fixtures';

function normalizeToken(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  if ((trimmed.startsWith("'") && trimmed.endsWith("'")) || (trimmed.startsWith('"') && trimmed.endsWith('"'))) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

const loginToken = normalizeToken(process.env.SMOKE_LOGIN_TOKEN);
const shouldRun = !!loginToken;

test.describe('Hosted Session Authentication - Live', () => {
  test.skip(!shouldRun, 'SMOKE_LOGIN_TOKEN required for hosted auth tests');

  test('accept-token sets cookie with expected attributes', async ({ playwright }) => {
    const apiUrl = process.env.API_URL || process.env.PLAYWRIGHT_API_BASE_URL || '';
    test.skip(!apiUrl, 'API_URL required');

    const authRequest = await playwright.request.newContext({
      baseURL: apiUrl,
      timeout: 30_000,
    });

    try {
      const loginRes = await authRequest.post('/api/auth/accept-token', {
        data: { token: loginToken },
      });
      expect(loginRes.status()).toBe(200);

      const setCookieHeader = loginRes.headers()['set-cookie'];
      expect(setCookieHeader).toBeDefined();

      const cookieString = setCookieHeader?.toLowerCase() || '';
      expect(cookieString).toMatch(/longhouse_session=/);
      expect(cookieString).toContain('httponly');
      expect(cookieString).toContain('samesite=lax');
      expect(cookieString).toContain('secure');

      const loginData = await loginRes.json();
      expect(loginData).toHaveProperty('access_token');
      expect(loginData).toHaveProperty('expires_in');
    } finally {
      await authRequest.dispose();
    }
  });

  test('accepted session cookie authenticates API requests', async ({ playwright }) => {
    const apiUrl = process.env.API_URL || process.env.PLAYWRIGHT_API_BASE_URL || '';
    test.skip(!apiUrl, 'API_URL required');

    const authRequest = await playwright.request.newContext({
      baseURL: apiUrl,
      timeout: 30_000,
    });

    try {
      const loginRes = await authRequest.post('/api/auth/accept-token', {
        data: { token: loginToken },
      });
      expect(loginRes.status()).toBe(200);

      const verifyRes = await authRequest.get('/api/auth/verify');
      expect(verifyRes.status()).toBe(204);
    } finally {
      await authRequest.dispose();
    }
  });

  test('unauthenticated request returns 401', async ({ playwright }) => {
    const apiUrl = process.env.API_URL || process.env.PLAYWRIGHT_API_BASE_URL || '';
    test.skip(!apiUrl, 'API_URL required');

    const anonymous = await playwright.request.newContext({
      baseURL: apiUrl,
      timeout: 30_000,
    });

    try {
      const response = await anonymous.get('/api/auth/verify');
      expect(response.status()).toBe(401);
    } finally {
      await anonymous.dispose();
    }
  });

  test('browser credential flow sends session cookie to hosted API', async ({ page, context }) => {
    const frontendUrl = process.env.FRONTEND_URL || process.env.PLAYWRIGHT_BASE_URL || '';
    const apiUrl = process.env.API_URL || process.env.PLAYWRIGHT_API_BASE_URL || frontendUrl;
    test.skip(!frontendUrl || !apiUrl, 'FRONTEND_URL and API_URL required');

    const cookies = await context.cookies();
    const sessionCookie = cookies.find((cookie) => cookie.name === 'longhouse_session');
    expect(sessionCookie).toBeDefined();

    await page.goto(frontendUrl);
    await page.waitForLoadState('domcontentloaded');

    const verifyStatus = await page.evaluate(async ({ targetApiUrl }) => {
      const response = await fetch(`${targetApiUrl}/api/auth/verify`, {
        credentials: 'include',
      });
      return response.status;
    }, { targetApiUrl: apiUrl });

    expect(verifyStatus).toBe(204);
  });
});
