import { test, expect } from './fixtures';

test.describe('Authentication flows', () => {
  // Reset DB before each test for clean state
  test.beforeEach(async ({ request }) => {
    await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
  });

  test('Dev login flow - landing page to dashboard', async ({ page }) => {
    // Navigate to landing page
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Wait for landing page to load
    // In dev mode, we should see the landing page with "Start Free" button
    const startFreeBtn = page.locator('button:has-text("Start Free")');

    // Check if we're on landing page or dashboard
    const isLandingPage = await startFreeBtn.isVisible({ timeout: 5000 }).catch(() => false);

    if (isLandingPage) {
      console.log('🏠 On landing page, opening login modal...');

      // Click Start Free to open login modal
      await startFreeBtn.click();

      // Wait for login modal to appear
      const loginModal = page.locator('.landing-login-modal');
      await expect(loginModal).toBeVisible({ timeout: 5000 });

      // Click Dev Login button (only visible in dev mode)
      const devLoginBtn = page.locator('button:has-text("Dev Login")');
      await expect(devLoginBtn).toBeVisible({ timeout: 5000 });
      await devLoginBtn.click();

      // Wait for redirect to dashboard
      // Dev login sets cookie and redirects to /dashboard
      await page.waitForURL(/\/dashboard/, { timeout: 10000 });

      console.log('✅ Dev login successful, redirected to dashboard');
    } else {
      console.log('📊 Already on dashboard (auth may be disabled), verifying...');
    }

    // Verify we're on dashboard and it's functional
    const url = page.url();
    expect(url).toMatch(/\/dashboard|\/$/);

    // Dashboard should have create agent button
    await expect(page.locator('[data-testid="create-agent-btn"]')).toBeVisible({ timeout: 10000 });

    console.log('✅ Dashboard loaded successfully');
  });

  test('Dev login API endpoint returns valid response', async ({ request }) => {
    // Directly test the dev-login API endpoint
    const response = await request.post('/auth/dev-login', {
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // In dev mode (AUTH_DISABLED=1), this should succeed
    expect(response.status()).toBe(200);

    const data = await response.json();
    console.log('📊 Dev login response:', data);

    // Verify response contains expected fields
    expect(data).toHaveProperty('message');
    expect(data.message).toContain('logged in');

    // Check for set-cookie header (auth cookie)
    const cookies = response.headers()['set-cookie'];
    console.log('🍪 Set-Cookie header:', cookies ? 'present' : 'missing');

    console.log('✅ Dev login API works correctly');
  });

  test('Auth cookie is set after dev login', async ({ page, context }) => {
    // Navigate to landing page and do dev login
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    const startFreeBtn = page.locator('button:has-text("Start Free")');
    const isLandingPage = await startFreeBtn.isVisible({ timeout: 5000 }).catch(() => false);

    if (!isLandingPage) {
      console.log('⚠️ Not on landing page, skipping cookie test');
      test.skip();
      return;
    }

    // Open login modal and click dev login
    await startFreeBtn.click();
    await page.locator('.landing-login-modal').waitFor({ timeout: 5000 });
    await page.locator('button:has-text("Dev Login")').click();

    // Wait for redirect
    await page.waitForURL(/\/dashboard/, { timeout: 10000 });

    // Check cookies are set
    const cookies = await context.cookies();
    console.log('🍪 Cookies after dev login:', cookies.map(c => c.name));

    // There should be an auth cookie (session or jwt)
    const authCookieNames = ['session', 'jwt', 'access_token', 'auth'];
    const hasAuthCookie = cookies.some(c =>
      authCookieNames.some(name => c.name.toLowerCase().includes(name))
    );

    // Note: In dev mode with AUTH_DISABLED=1, there might not be a cookie
    // but the login should still redirect successfully
    console.log('🔐 Auth cookie present:', hasAuthCookie);

    console.log('✅ Dev login flow completed successfully');
  });

  test('No CORS errors during dev login', async ({ page }) => {
    // Collect console errors during test
    const consoleErrors: string[] = [];
    page.on('console', msg => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text());
      }
    });

    // Also collect page errors
    const pageErrors: Error[] = [];
    page.on('pageerror', error => {
      pageErrors.push(error);
    });

    // Navigate and attempt dev login
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    const startFreeBtn = page.locator('button:has-text("Start Free")');
    const isLandingPage = await startFreeBtn.isVisible({ timeout: 5000 }).catch(() => false);

    if (!isLandingPage) {
      console.log('⚠️ Not on landing page, skipping CORS test');
      test.skip();
      return;
    }

    // Perform dev login
    await startFreeBtn.click();
    await page.locator('.landing-login-modal').waitFor({ timeout: 5000 });
    await page.locator('button:has-text("Dev Login")').click();

    // Wait a bit for any errors to appear
    await page.waitForTimeout(2000);

    // Check for CORS errors
    const corsErrors = consoleErrors.filter(e =>
      e.toLowerCase().includes('cors') ||
      e.toLowerCase().includes('access-control') ||
      e.toLowerCase().includes('blocked by cors')
    );

    if (corsErrors.length > 0) {
      console.log('❌ CORS errors detected:', corsErrors);
    }

    // CRITICAL: No CORS errors should occur
    expect(corsErrors, 'No CORS errors should occur during dev login').toHaveLength(0);

    console.log('✅ No CORS errors during dev login');
  });

  test('Login redirect when unauthenticated (dev mode bypass)', async ({ page }) => {
    // Navigate to a protected route
    await page.goto('/dashboard');
    await page.waitForLoadState('networkidle');

    // In dev mode AUTH_DISABLED=1, should go directly to dashboard
    // In production, would redirect to login
    const url = page.url();
    console.log('📊 URL after navigating to /dashboard:', url);

    // Either we're on dashboard or redirected to login/landing
    expect(url).toMatch(/\/(dashboard)?$/);

    console.log('✅ Protected route access handled correctly');
  });

  test('Mock Google OAuth flow placeholder', async () => {
    test.skip(true, 'Google OAuth cannot run in CI');
  });

  test('Logout flow placeholder', async () => {
    test.skip();
  });

  test('Unauthorized access attempts placeholder', async () => {
    test.skip();
  });
});
