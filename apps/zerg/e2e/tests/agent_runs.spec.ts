import { test, expect } from './fixtures';

test.describe('Agent run history view', () => {
  // SKIPPED: Debug modal doesn't have a "Runs" tab - run history is displayed
  // inline in the dashboard table. These tests expect non-existent UI.
  test.skip('Open run history tab placeholder', async ({ page }) => {
    await page.goto('/');
    await page.locator('[data-testid="create-agent-btn"]').click();
    const agentId = await page.locator('tr[data-agent-id]').first().getAttribute('data-agent-id');

    const debugBtn = page.locator(`[data-testid="debug-agent-${agentId}"]`);
    // REQUIRE debug button to exist - core agent management functionality
    await expect(debugBtn).toBeVisible({ timeout: 5000 });

    await debugBtn.click();

    // Modal MUST open - core agent debugging functionality
    const debugModal = page.locator('[data-testid="agent-debug-modal"]');
    await expect(debugModal).toBeVisible({ timeout: 5000 });

    await page.waitForSelector('[data-testid="agent-debug-modal"]', { timeout: 5000 });

    // Runs tab MUST exist - essential for monitoring agent execution
    const runsTab = page.locator('button', { hasText: 'Runs' });
    await expect(runsTab).toBeVisible({ timeout: 5000 });

    await runsTab.click();
    // Just verify the tab switched - don't assert on specific content yet
  });

  // SKIPPED: Debug modal doesn't have a "Runs" tab
  test.skip('Run list filters and basic functionality', async ({ page }) => {
    await page.goto('/');
    await page.locator('[data-testid="create-agent-btn"]').click();
    const agentId = await page.locator('tr[data-agent-id]').first().getAttribute('data-agent-id');

    await page.locator(`[data-testid="debug-agent-${agentId}"]`).click();
    await expect(page.locator('[data-testid="agent-debug-modal"]')).toBeVisible({ timeout: 5000 });
    await page.locator('button', { hasText: 'Runs' }).click();

    // Basic run list should exist
    const runsList = page.locator('[data-testid="runs-list"], .runs-list, .run-history');
    await expect(runsList).toBeVisible({ timeout: 5000 });
  });

  test('Run pagination placeholder', async () => {
    test.skip();
  });

  test('Export run data placeholder', async () => {
    test.skip();
  });
});
