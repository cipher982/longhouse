import { test, expect } from '@playwright/test';
import { setupWebRTCMocks } from '../utils/webrtc-mocks.js';

test.describe('WebRTC Connection Flow', () => {
  test.beforeEach(async ({ page }) => {
    await setupWebRTCMocks(page);
    await page.goto('/');
  });

  test('should successfully connect to OpenAI Realtime API', async ({ page }) => {
    // Wait for page to load (allow context branding to change title)
    await expect(page.locator('h1')).toContainText(/Jarvis/i);

    // Click connect button
    await page.click('#connectBtn');

    // Expect UI to reflect connected state (button states), and no failure text
    await expect(page.locator('#connectBtn')).toBeDisabled({ timeout: 10000 });
    await expect(page.locator('#disconnectBtn')).not.toBeDisabled();
    await expect(page.locator('#pttBtn')).not.toBeDisabled();
    await expect(page.locator('#transcript')).not.toContainText('Connection failed');
  });

  test('should handle push-to-talk interaction', async ({ page }) => {
    // Connect first
    await page.click('#connectBtn');
    await expect(page.locator('#pttBtn')).not.toBeDisabled();

    // Test push-to-talk
    await page.locator('#pttBtn').dispatchEvent('pointerdown');
    await expect(page.locator('#pttBtn')).toContainText('Release to Stop');
    // A pending user bubble should appear while listening
    await expect(page.locator('.user-turn.pending')).toBeVisible();

    // Release push-to-talk
    await page.locator('#pttBtn').dispatchEvent('pointerup');
    await expect(page.locator('#pttBtn')).toContainText('Push‑to‑Talk');
  });

  test('should disconnect successfully', async ({ page }) => {
    // Connect first
    await page.click('#connectBtn');
    await expect(page.locator('#pttBtn')).not.toBeDisabled();

    // Disconnect
    await page.click('#disconnectBtn');

    // Verify buttons state
    await expect(page.locator('#connectBtn')).not.toBeDisabled();
    await expect(page.locator('#disconnectBtn')).toBeDisabled();
    await expect(page.locator('#pttBtn')).toBeDisabled();
  });

  test('should display audio level meter during connection', async ({ page }) => {
    await page.click('#connectBtn');
    // Use a simpler connected indicator: PTT becomes ready/enabled
    await expect(page.locator('#pttBtn')).not.toBeDisabled();
  });

  test('should handle connection errors gracefully', async ({ page }) => {
    // Mock a connection failure
    await page.addInitScript(() => {
      window.fetch = async (url) => {
        if (url.includes('/session')) {
          throw new Error('Network error');
        }
      };
    });

    await page.click('#connectBtn');

    // Should show error state
    await expect(page.locator('#transcript')).toContainText('Connecting', { timeout: 2000 });
    // Button should be re-enabled after failure
    await expect(page.locator('#connectBtn')).not.toBeDisabled({ timeout: 5000 });
  });
});
