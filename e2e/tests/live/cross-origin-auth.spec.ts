/**
 * Hosted session authentication smoke - Live suite.
 *
 * Verifies a hosted browser/device credential can authenticate API/browser
 * requests and that the browser carries the canonical session cookie to the
 * hosted API.
 *
 * REQUIRES: SMOKE_RUNTIME_TOKEN or LONGHOUSE_DEVICE_TOKEN.
 */

import { test, expect, normalizeToken } from './fixtures';

const authCredential =
  normalizeToken(process.env.SMOKE_RUNTIME_TOKEN) ||
  normalizeToken(process.env.LONGHOUSE_DEVICE_TOKEN);
const shouldRun = !!authCredential;
test.describe('Hosted Session Authentication - Live', () => {
  test.skip(!shouldRun, 'SMOKE_RUNTIME_TOKEN or LONGHOUSE_DEVICE_TOKEN required for hosted auth tests');

  test('runtime token authenticates API requests', async ({ playwright }) => {
    const apiUrl = process.env.API_URL || process.env.PLAYWRIGHT_API_BASE_URL || '';
    test.skip(!apiUrl, 'API_URL required');

    const authRequest = await playwright.request.newContext({
      baseURL: apiUrl,
      timeout: 30_000,
      extraHTTPHeaders: {
        Authorization: `Bearer ${authCredential}`,
      },
    });

    try {
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
    const secure = new URL(apiUrl).protocol === 'https:';
    const sessionCookie = cookies.find(
      (cookie) => cookie.name === (secure ? '__Host-lh_session' : 'longhouse_session'),
    );
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
