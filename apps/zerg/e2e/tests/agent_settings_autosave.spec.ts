/**
 * Agent Settings Drawer - Auto-Save & Optimistic Updates E2E Tests
 *
 * Tests critical fixes for:
 * 1. Optimistic update rollback on failure (bug fix 016d627)
 * 2. Unified close guards for all 4 close paths (bug fix f736c59)
 * 3. Race condition prevention via mutation mutex (bug fix 016d627)
 */

import { test, expect, type Page } from './fixtures';
import { createAgentViaAPI, deleteAgentViaAPI } from './helpers/agent-helpers';
import { resetDatabaseViaRequest } from './helpers/database-helpers';
import { safeClick, waitForStableElement } from './helpers/test-utils';
import { waitForToast } from './helpers/test-helpers';

test.describe('Agent Settings Drawer - Auto-Save', () => {
  let testAgentId: number;

  test.beforeEach(async ({ page }, testInfo) => {
    const workerId = String(testInfo.workerIndex);

    // Reset database for clean state
    await resetDatabaseViaRequest(page, { workerId });

    // Create test agent with no allowed tools
    const agent = await createAgentViaAPI(workerId, {
      name: 'Auto-Save Test Agent',
      allowed_tools: [],
    });
    testAgentId = agent.id;

    // Navigate to dashboard
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Click dashboard tab if not already there
    const dashboardTab = page.locator('[data-testid="global-dashboard-tab"]');
    if (await dashboardTab.isVisible()) {
      await dashboardTab.click();
    }
  });

  test.afterEach(async ({ page }, testInfo) => {
    const workerId = String(testInfo.workerIndex);
    if (testAgentId) {
      await deleteAgentViaAPI(workerId, testAgentId);
    }
  });

  /**
   * Helper: Opens the agent settings drawer
   */
  async function openSettingsDrawer(page: Page, agentId: number) {
    // Find and click the agent card settings button
    const settingsButton = page.locator(`[data-testid="edit-agent-${agentId}"]`).first();
    await safeClick(page, settingsButton, { maxAttempts: 3 });

    // Wait for drawer to slide in and stabilize
    const drawer = page.locator('.agent-settings-drawer.open');
    await expect(drawer).toBeVisible({ timeout: 3000 });
    await waitForStableElement(page, '.agent-settings-drawer');
  }

  /**
   * Helper: Gets the first tool checkbox in the Allowed Tools section
   */
  async function getFirstToolCheckbox(page: Page) {
    // Locate the Allowed Tools section
    const allowedToolsSection = page.locator('.agent-settings-section', {
      has: page.locator('h3:has-text("Allowed Tools")'),
    });

    // Get the first checkbox within this section
    const checkbox = allowedToolsSection.locator('input[type="checkbox"]').first();
    await expect(checkbox).toBeVisible({ timeout: 2000 });
    return checkbox;
  }

  /**
   * Helper: Waits for save indicator to appear and disappear
   */
  async function waitForSaveToComplete(page: Page, timeoutMs = 3000) {
    const saveIndicator = page.locator('.saving-indicator');

    // Wait for indicator to appear (debounce completes)
    try {
      await expect(saveIndicator).toBeVisible({ timeout: 1000 });
    } catch {
      // Indicator might be too fast to catch - that's ok
    }

    // Wait for indicator to disappear (mutation completes)
    await expect(saveIndicator).not.toBeVisible({ timeout: timeoutMs });
  }

  test('should debounce rapid tool toggles and auto-save after 500ms', async ({ page }) => {
    await openSettingsDrawer(page, testAgentId);

    const checkbox = await getFirstToolCheckbox(page);
    const initialState = await checkbox.isChecked();

    // Rapid toggles (should collapse into single save)
    await checkbox.click(); // Toggle 1
    await page.waitForTimeout(100);
    await checkbox.click(); // Toggle 2 (cancels first debounce)
    await page.waitForTimeout(100);
    await checkbox.click(); // Toggle 3 (cancels second debounce)

    // Final state should be opposite of initial
    const expectedState = !initialState;
    await expect(checkbox).toBeChecked({ checked: expectedState });

    // Wait for debounce window to expire and save to complete
    await waitForSaveToComplete(page);

    // Close drawer
    const closeButton = page.locator('.agent-settings-footer button:has-text("Close")');
    await closeButton.click();
    await expect(page.locator('.agent-settings-drawer.open')).not.toBeVisible();

    // Reopen drawer to verify persistence
    await openSettingsDrawer(page, testAgentId);
    const checkboxAfterReopen = await getFirstToolCheckbox(page);
    await expect(checkboxAfterReopen).toBeChecked({ checked: expectedState });
  });

  test('should show save indicator during debounce and mutation', async ({ page }) => {
    await openSettingsDrawer(page, testAgentId);

    const checkbox = await getFirstToolCheckbox(page);
    const saveIndicator = page.locator('.saving-indicator');

    // Initially not visible
    await expect(saveIndicator).not.toBeVisible();

    // Toggle checkbox
    await checkbox.click();

    // Save indicator should appear during mutation
    await expect(saveIndicator).toBeVisible({ timeout: 1000 });

    // And then disappear after save completes
    await expect(saveIndicator).not.toBeVisible({ timeout: 3000 });
  });

  test('should queue changes during in-flight mutation (race condition fix)', async ({ page }) => {
    await openSettingsDrawer(page, testAgentId);

    const checkbox = await getFirstToolCheckbox(page);
    const initialChecked = await checkbox.isChecked();

    // Toggle and immediately try another toggle
    await checkbox.click();

    // Wait for mutation to start (debounce expires)
    await page.waitForTimeout(600);

    // Verify save indicator is showing (mutation in-flight)
    // This assertion MUST pass - if it doesn't, the test is invalid
    const saveIndicator = page.locator('.saving-indicator');
    await expect(saveIndicator).toBeVisible({ timeout: 1000 });

    // Toggle again during in-flight mutation (should queue silently)
    await checkbox.click();

    // UI should reflect the queued change
    await expect(checkbox).toBeChecked({ checked: initialChecked });

    // Wait for first mutation to complete
    await expect(saveIndicator).not.toBeVisible({ timeout: 3000 });

    // Queued change should fire next
    await expect(saveIndicator).toBeVisible({ timeout: 1000 });
    await expect(saveIndicator).not.toBeVisible({ timeout: 3000 });

    // Close and reopen to verify final state persisted
    const closeButton = page.locator('.agent-settings-footer button:has-text("Close")');
    await closeButton.click();
    await openSettingsDrawer(page, testAgentId);

    const checkboxAfterReopen = await getFirstToolCheckbox(page);
    // Final state should be back to initial (toggled twice)
    await expect(checkboxAfterReopen).toBeChecked({ checked: initialChecked });
  });

  test('should rollback optimistic update on API error', async ({ page }) => {
    await openSettingsDrawer(page, testAgentId);

    const checkbox = await getFirstToolCheckbox(page);
    const initialChecked = await checkbox.isChecked();

    // Intercept the PUT request and force it to fail
    await page.route('**/api/agents/*', (route) => {
      if (route.request().method() === 'PUT') {
        route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Simulated server error' }),
        });
      } else {
        route.continue();
      }
    });

    // Toggle checkbox (optimistic update)
    await checkbox.click();

    // UI should update immediately (optimistic)
    await expect(checkbox).toBeChecked({ checked: !initialChecked });

    // Wait for mutation to fail
    await page.waitForTimeout(1000);

    // Should show error toast
    await waitForToast(page, 'Failed to update tools', { timeout: 2000, type: 'error' });

    // Checkbox should ROLLBACK to initial state
    await expect(checkbox).toBeChecked({ checked: initialChecked }, { timeout: 2000 });

    // Unroute to restore normal behavior
    await page.unroute('**/api/agents/*');
  });

  test.describe('Close Path Guards (4 paths)', () => {
    /**
     * Test that all 4 close mechanisms guard against closing with in-flight saves:
     * 1. Header × button
     * 2. Backdrop click
     * 3. ESC key
     * 4. Footer Close button
     */

    async function startSaveAndTestCloseGuard(
      page: Page,
      closeAction: () => Promise<void>,
      closeName: string
    ) {
      await openSettingsDrawer(page, testAgentId);

      const checkbox = await getFirstToolCheckbox(page);

      // Toggle to trigger save
      await checkbox.click();

      // Wait for debounce to expire and mutation to start
      await page.waitForTimeout(600);

      // Intercept PUT to make it slow (simulate in-flight mutation)
      await page.route('**/api/agents/*', async (route) => {
        if (route.request().method() === 'PUT') {
          // Delay response by 2 seconds
          await page.waitForTimeout(2000);
          route.continue();
        } else {
          route.continue();
        }
      });

      // Toggle again to ensure mutation is in-flight
      await checkbox.click();
      await page.waitForTimeout(600);

      // Verify save indicator is visible (mutation in-flight)
      const saveIndicator = page.locator('.saving-indicator');
      await expect(saveIndicator).toBeVisible({ timeout: 1000 });

      // Set up dialog handler to cancel close
      page.once('dialog', async (dialog) => {
        expect(dialog.message()).toContain('Save in progress');
        await dialog.dismiss(); // Cancel the close
      });

      // Attempt to close
      await closeAction();

      // Drawer should still be visible (close was cancelled)
      await expect(page.locator('.agent-settings-drawer.open')).toBeVisible();

      // Clean up route
      await page.unroute('**/api/agents/*');
    }

    test('should guard header × button close with pending save', async ({ page }) => {
      await startSaveAndTestCloseGuard(
        page,
        async () => {
          const headerCloseBtn = page.locator('.agent-settings-header .close-btn');
          await headerCloseBtn.click();
        },
        'Header × button'
      );
    });

    test('should guard backdrop click close with pending save', async ({ page }) => {
      await startSaveAndTestCloseGuard(
        page,
        async () => {
          // Click backdrop (outside drawer)
          const backdrop = page.locator('.agent-settings-backdrop.open');
          await backdrop.click({ position: { x: 10, y: 10 } });
        },
        'Backdrop click'
      );
    });

    test('should guard ESC key close with pending save', async ({ page }) => {
      await startSaveAndTestCloseGuard(
        page,
        async () => {
          await page.keyboard.press('Escape');
        },
        'ESC key'
      );
    });

    test('should guard footer Close button with pending save', async ({ page }) => {
      await startSaveAndTestCloseGuard(
        page,
        async () => {
          const footerCloseBtn = page.locator('.agent-settings-footer button:has-text("Close")');
          await footerCloseBtn.click();
        },
        'Footer Close button'
      );
    });

    test('should warn about unsaved debounced changes on close', async ({ page }) => {
      await openSettingsDrawer(page, testAgentId);

      const checkbox = await getFirstToolCheckbox(page);

      // Toggle to start debounce timer (but don't wait for it to fire)
      await checkbox.click();
      await page.waitForTimeout(200); // Only 200ms, debounce is 500ms

      // Set up dialog handler to choose "No" (discard changes)
      page.once('dialog', async (dialog) => {
        expect(dialog.message()).toContain('unsaved changes');
        await dialog.dismiss(); // Choose "No" (discard)
      });

      // Close (should prompt user)
      const closeButton = page.locator('.agent-settings-footer button:has-text("Close")');
      await closeButton.click();

      // Drawer should close (user chose to discard)
      await expect(page.locator('.agent-settings-drawer.open')).not.toBeVisible();

      // Reopen and verify checkbox did NOT save (user discarded)
      await openSettingsDrawer(page, testAgentId);
      const checkboxAfterReopen = await getFirstToolCheckbox(page);
      await expect(checkboxAfterReopen).toBeChecked({ checked: false });
    });

    test('should flush pending debounce when user chooses to save on close', async ({ page }) => {
      await openSettingsDrawer(page, testAgentId);

      const checkbox = await getFirstToolCheckbox(page);

      // Toggle to start debounce timer
      await checkbox.click();
      await page.waitForTimeout(200); // Only 200ms, debounce is 500ms

      // Set up dialog handler to choose "Yes" (save)
      page.once('dialog', async (dialog) => {
        expect(dialog.message()).toContain('unsaved changes');
        await dialog.accept(); // Choose "Yes" (save)
      });

      // Close (should prompt and flush)
      const closeButton = page.locator('.agent-settings-footer button:has-text("Close")');
      await closeButton.click();

      // Drawer should close, mutation happens in background
      await expect(page.locator('.agent-settings-drawer.open')).not.toBeVisible();

      // Wait a moment for background save
      await page.waitForTimeout(1000);

      // Reopen and verify checkbox DID save (user chose to save)
      await openSettingsDrawer(page, testAgentId);
      const checkboxAfterReopen = await getFirstToolCheckbox(page);
      await expect(checkboxAfterReopen).toBeChecked({ checked: true });
    });
  });

  test('should persist changes across drawer open/close cycles', async ({ page }) => {
    // First cycle: toggle and save
    await openSettingsDrawer(page, testAgentId);
    const checkbox1 = await getFirstToolCheckbox(page);
    await checkbox1.click();
    await waitForSaveToComplete(page);

    const closeButton1 = page.locator('.agent-settings-footer button:has-text("Close")');
    await closeButton1.click();
    await expect(page.locator('.agent-settings-drawer.open')).not.toBeVisible();

    // Second cycle: verify persistence
    await openSettingsDrawer(page, testAgentId);
    const checkbox2 = await getFirstToolCheckbox(page);
    await expect(checkbox2).toBeChecked();

    // Toggle back and save
    await checkbox2.click();
    await waitForSaveToComplete(page);

    const closeButton2 = page.locator('.agent-settings-footer button:has-text("Close")');
    await closeButton2.click();
    await expect(page.locator('.agent-settings-drawer.open')).not.toBeVisible();

    // Third cycle: verify second change persisted
    await openSettingsDrawer(page, testAgentId);
    const checkbox3 = await getFirstToolCheckbox(page);
    await expect(checkbox3).not.toBeChecked();
  });

  test('should handle custom tool addition with auto-save', async ({ page }) => {
    await openSettingsDrawer(page, testAgentId);

    // Find custom tool input
    const customToolInput = page.locator('.custom-tool-input input[type="text"]');
    const addButton = page.locator('.custom-tool-input button:has-text("Add")');

    await customToolInput.fill('http_*');
    await addButton.click();

    // Wait for auto-save
    await waitForSaveToComplete(page);

    // Verify the custom tool appears as a checkbox
    const customToolCheckbox = page.locator('.tool-option', {
      has: page.locator('span:has-text("http_*")'),
    }).locator('input[type="checkbox"]');

    await expect(customToolCheckbox).toBeVisible();
    await expect(customToolCheckbox).toBeChecked();

    // Close and reopen to verify persistence
    const closeButton = page.locator('.agent-settings-footer button:has-text("Close")');
    await closeButton.click();
    await openSettingsDrawer(page, testAgentId);

    const customToolCheckboxAfterReopen = page.locator('.tool-option', {
      has: page.locator('span:has-text("http_*")'),
    }).locator('input[type="checkbox"]');

    await expect(customToolCheckboxAfterReopen).toBeVisible();
    await expect(customToolCheckboxAfterReopen).toBeChecked();
  });
});
