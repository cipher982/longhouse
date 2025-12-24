import { test, expect } from './fixtures';

/**
 * Canvas UI Tests
 * These tests ensure the canvas feature works correctly from a user perspective.
 * API contract validation is handled by backend tests (test_api_contract_canvas.py)
 */
test.describe('Canvas UI Tests', () => {

  test('Canvas tab loads successfully without errors', async ({ page }) => {
    // User navigates to the app
    await page.goto('/');
    await page.waitForFunction(() => (window as any).__APP_READY__ === true, { timeout: 15000 });

    // Wait for app header/tabs to render
    await expect(page.locator('.header-nav')).toBeVisible({ timeout: 15000 });
    // User clicks on Canvas tab
    const canvasTab = page.getByTestId('global-canvas-tab');
    await expect(canvasTab).toBeVisible({ timeout: 15000 });
    await canvasTab.click();

    // Canvas should load without errors
    // Check for common canvas elements that indicate successful load
    const canvasContainer = page.locator('#canvas-container, [data-testid="canvas-container"], .canvas-wrapper');
    await expect(canvasContainer).toBeVisible({ timeout: 5000 });

    // No error messages should be present
    const errorElements = page.locator('.error-message, .error-banner, [data-testid="error"], .alert-danger');
    await expect(errorElements).not.toBeVisible();

    // Agent/Tool shelf should be visible (left panel)
    const shelf = page.locator('#agent-shelf');
    await expect(shelf).toBeVisible();
  });

  test('Canvas persists state when switching tabs', async ({ page }) => {
    // Navigate to canvas
    await page.goto('/');
    await page.waitForFunction(() => (window as any).__APP_READY__ === true, { timeout: 15000 });
    await expect(page.getByTestId('global-canvas-tab')).toBeVisible({ timeout: 15000 });
    await page.getByTestId('global-canvas-tab').click();

    // Wait for canvas to load
    const canvasContainer = page.locator('#canvas-container, [data-testid="canvas-container"], .canvas-wrapper');
    await expect(canvasContainer).toBeVisible({ timeout: 5000 });

    // Switch to dashboard and back
    await page.locator('.header-nav').click();
    await page.waitForTimeout(500);
    await page.getByTestId('global-canvas-tab').click();

    // Canvas should still be visible without errors
    await expect(canvasContainer).toBeVisible({ timeout: 5000 });
    const errorElements = page.locator('.error-message, .error-banner, [data-testid="error"]');
    await expect(errorElements).not.toBeVisible();
  });

  test('Canvas workspace is interactive', async ({ page }) => {
    // Navigate to canvas
    await page.goto('/');
    await page.waitForFunction(() => (window as any).__APP_READY__ === true, { timeout: 15000 });
    await expect(page.getByTestId('global-canvas-tab')).toBeVisible({ timeout: 15000 });
    await page.getByTestId('global-canvas-tab').click();

    // Wait for canvas to load
    const canvasContainer = page.locator('[data-testid="canvas-container"], #canvas-container');
    await expect(canvasContainer).toBeVisible({ timeout: 10000 });

    // Click within the canvas container to validate interaction
    const box = await canvasContainer.boundingBox();
    if (box) {
      await page.mouse.click(box.x + Math.min(100, box.width / 2), box.y + Math.min(100, box.height / 2));
    }

    // Still no errors after interaction
    const errorElements = page.locator('.error-message, .error-banner');
    await expect(errorElements).not.toBeVisible();
  });
});
