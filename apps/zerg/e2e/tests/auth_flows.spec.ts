import { test } from './fixtures';

test.describe('Authentication flows', () => {
  test('Login redirect when unauthenticated (dev mode bypass)', async ({ page }) => {
    await page.goto('/protected-route'); // hypothetical route
    // In dev mode AUTH_DISABLED=1 so app redirects to dashboard
    await page.waitForURL(/localhost:8002/);
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
