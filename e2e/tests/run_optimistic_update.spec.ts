/**
 * E2E Test: Run Button Real-time Updates
 *
 * Validates that clicking the run button results in immediate feedback
 * via optimistic updates, with WebSocket providing authoritative status
 * confirmation and real-time multi-user synchronization.
 *
 * This tests the hybrid optimistic + WebSocket approach.
 */

import { test, expect } from './fixtures';
import { resetDatabase } from './test-utils';

test.describe('Run Button Real-time Update', () => {
  // Reset database before each test
  // Uses strict reset that throws on failure to fail fast
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  async function createAutomationAndGetId(page: any): Promise<string> {
    const createBtn = page.locator('[data-testid="create-automation-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 10000 });

    const [response] = await Promise.all([
      page.waitForResponse(
        (r: any) => r.url().includes('/api/automations') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    const body = await response.json();
    const automationId = String(body.id);
    if (!automationId || automationId === 'undefined') {
      throw new Error(`Failed to get automation ID from API response: ${JSON.stringify(body)}`);
    }

    const automationRow = page.locator(`tr[data-automation-id="${automationId}"]`);
    await expect(automationRow).toBeVisible({ timeout: 5000 });
    return automationId;
  }

  test('should transition to running via optimistic update and websocket', async ({ page }) => {
    await page.goto('/automations');

    // Slow down run requests so optimistic UI has time to render Running
    await page.route('**/api/automations/*/task', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 750));
      await route.continue();
    });

    const automationId = await createAutomationAndGetId(page);
    const automationRow = page.locator(`tr[data-automation-id="${automationId}"]`);

    // Get initial status
    const statusCell = automationRow.locator('td[data-label="Status"]');
    await expect(statusCell).toContainText('Idle', { timeout: 5000 });

    // Find and click the run button
    const runButton = page.locator(`[data-testid="run-automation-${automationId}"]`);
    await expect(runButton).toBeVisible({ timeout: 5000 });
    await runButton.click();

    // Optimistic update should be immediate, WebSocket confirms shortly after.
    // Use reasonable timeout to handle backend load variance in parallel test runs.
    await expect(statusCell).toHaveText(/Running/, { timeout: 10000 });

    // Verify the run button is disabled during the run
    await expect(runButton).toBeDisabled({ timeout: 5000 });
  });

  test('should handle run button clicks with multiple automations', async ({ page }) => {
    await page.goto('/automations');

    // Slow down run requests so optimistic UI has time to render Running
    await page.route('**/api/automations/*/task', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 750));
      await route.continue();
    });

    const firstAutomationId = await createAutomationAndGetId(page);
    const secondAutomationId = await createAutomationAndGetId(page);

    const firstAutomationRow = page.locator(`tr[data-automation-id="${firstAutomationId}"]`);
    const secondAutomationRow = page.locator(`tr[data-automation-id="${secondAutomationId}"]`);

    // Get both status cells
    const firstStatusCell = firstAutomationRow.locator('td[data-label="Status"]');
    const secondStatusCell = secondAutomationRow.locator('td[data-label="Status"]');

    // Both should start as Idle
    await expect(firstStatusCell).toContainText('Idle', { timeout: 5000 });
    await expect(secondStatusCell).toContainText('Idle', { timeout: 5000 });

    const firstRunButton = page.locator(`[data-testid="run-automation-${firstAutomationId}"]`);
    await expect(firstRunButton).toBeVisible({ timeout: 5000 });
    await firstRunButton.click();

    // Verify only the first automation's status changes.
    await expect(firstStatusCell).toHaveText(/Running/, { timeout: 10000 });
    await expect(secondStatusCell).toContainText('Idle', { timeout: 5000 });

    const secondRunButton = page.locator(`[data-testid="run-automation-${secondAutomationId}"]`);
    await expect(secondRunButton).toBeVisible({ timeout: 5000 });
    await secondRunButton.click();

    // Verify the second automation's status also changes.
    await expect(secondStatusCell).toHaveText(/Running/, { timeout: 10000 });
  });

  test('should rollback optimistic update when API call fails', async ({ page }) => {
    await page.goto('/automations');

    const createBtn = page.locator('[data-testid="create-automation-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 10000 });

    // Create an automation with deterministic wait.
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/automations') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    const automationRow = page.locator('tr[data-automation-id]').last();
    await expect(automationRow).toBeVisible({ timeout: 5000 });

    const automationId = await automationRow.getAttribute('data-automation-id');
    const statusCell = automationRow.locator('td[data-label="Status"]');

    // Verify initial status
    await expect(statusCell).toContainText('Idle', { timeout: 5000 });

    // Find the run button
    const runButton = page.locator(`[data-testid="run-automation-${automationId}"]`);
    await expect(runButton).toBeVisible({ timeout: 5000 });

    // Mock the API to fail (and assert the route is actually hit to avoid false positives)
    let taskRequests = 0;
    await page.route('**/api/automations/*/task', async (route) => {
      taskRequests += 1;
      await route.abort('failed');
    });

    // Click the run button
    await runButton.click();

    // Ensure our failure route was actually used
    await expect
      .poll(async () => taskRequests, { timeout: 5000, message: 'Expected run task request to be intercepted' })
      .toBeGreaterThan(0);

    // Wait for status to rollback to Idle
    // The optimistic update shows Running immediately, then rollback happens after API error
    await expect.poll(
      async () => await statusCell.textContent(),
      { timeout: 10000, message: 'Status should rollback to Idle after API error' }
    ).toContain('Idle');

    // Run button should remain usable after failure (no stuck pending state)
    await expect(runButton).toBeEnabled({ timeout: 5000 });
  });
});
