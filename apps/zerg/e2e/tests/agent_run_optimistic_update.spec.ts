/**
 * E2E Test: Agent Run Button Real-time Updates
 *
 * Validates that clicking the run button results in immediate feedback
 * via optimistic updates, with WebSocket providing authoritative status
 * confirmation and real-time multi-user synchronization.
 *
 * This tests the hybrid optimistic + WebSocket approach.
 */

import { test, expect } from './fixtures';

test.describe('Agent Run Button Real-time Update', () => {
  // Reset database before each test to prevent pollution from previous runs
  test.beforeEach(async ({ request }) => {
    await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
  });

  test('should transition to running via optimistic update and websocket', async ({ page }) => {
    // Navigate to dashboard
    await page.goto('/dashboard');
    await page.waitForLoadState('networkidle');

    // Create a new agent
    await page.locator('[data-testid="create-agent-btn"]').click();

    // Wait for the new agent row to appear (use last() to get most recent)
    const agentRow = page.locator('tr[data-agent-id]').last();
    await expect(agentRow).toBeVisible({ timeout: 5000 });

    const agentId = await agentRow.getAttribute('data-agent-id');
    expect(agentId).toBeTruthy();

    // Get initial status
    const statusCell = agentRow.locator('td[data-label="Status"]');
    const initialStatus = await statusCell.textContent();
    expect(initialStatus).toContain('Idle'); // Should start as idle

    // Find and click the run button for this agent
    const runButton = page.locator(`[data-testid="run-agent-${agentId}"]`);
    await expect(runButton).toBeVisible();

    // Click the run button
    await runButton.click();

    // Optimistic update should be immediate, WebSocket confirms shortly after.
    // Use 5s timeout to handle backend load variance in parallel test runs.
    await expect(statusCell).toHaveText(/Running/, { timeout: 5000 });

    // Verify the run button is disabled during the run
    await expect(runButton).toBeDisabled();

    console.log('✅ Hybrid optimistic + WebSocket update test passed');
  });

  test('should handle run button click with multiple agents', async ({ page }) => {
    // Navigate to dashboard
    await page.goto('/dashboard');
    await page.waitForLoadState('networkidle');

    // Create two agents
    await page.locator('[data-testid="create-agent-btn"]').click();
    await page.waitForTimeout(300);
    await page.locator('[data-testid="create-agent-btn"]').click();

    // Wait for both agents to appear
    const agentRows = page.locator('tr[data-agent-id]');
    await expect(agentRows).toHaveCount(2, { timeout: 5000 });

    // Get the two most recent agents (last two rows)
    const firstAgentRow = agentRows.nth(-2); // Second to last
    const secondAgentRow = agentRows.last(); // Last

    const firstAgentId = await firstAgentRow.getAttribute('data-agent-id');
    const secondAgentId = await secondAgentRow.getAttribute('data-agent-id');

    // Get both status cells from the correct rows
    const firstStatusCell = firstAgentRow.locator('td[data-label="Status"]');
    const secondStatusCell = secondAgentRow.locator('td[data-label="Status"]');

    // Click run on the first agent
    const firstRunButton = page.locator(`[data-testid="run-agent-${firstAgentId}"]`);
    await firstRunButton.click();

    // Verify ONLY the first agent's status changes
    await expect(firstStatusCell).toHaveText(/Running/, { timeout: 1500 });
    await expect(secondStatusCell).toHaveText(/Idle/);

    // Now click run on the second agent
    const secondRunButton = page.locator(`[data-testid="run-agent-${secondAgentId}"]`);
    await secondRunButton.click();

    // Verify the second agent's status also changes
    await expect(secondStatusCell).toHaveText(/Running/, { timeout: 1500 });

    console.log('✅ Multiple agent hybrid update test passed');
  });

  test('should rollback optimistic update when API call fails', async ({ page }) => {
    // Navigate to dashboard
    await page.goto('/dashboard');
    await page.waitForLoadState('networkidle');

    // Create an agent
    await page.locator('[data-testid="create-agent-btn"]').click();

    // Wait for the new agent row
    const agentRow = page.locator('tr[data-agent-id]').last();
    await expect(agentRow).toBeVisible({ timeout: 5000 });

    const agentId = await agentRow.getAttribute('data-agent-id');
    const statusCell = agentRow.locator('td[data-label="Status"]');

    // Get initial status
    const initialStatus = await statusCell.textContent();
    expect(initialStatus).toContain('Idle');

    // Find the run button
    const runButton = page.locator(`[data-testid="run-agent-${agentId}"]`);

    // Mock the API to fail
    await page.route('**/api/agents/*/task', async route => {
      // Abort the request to simulate an error
      await route.abort('failed');
    });

    // Click the run button
    await runButton.click();

    // Wait a bit for optimistic update to be attempted and rolled back
    await page.waitForTimeout(1000);

    // The status should be rolled back to Idle after the API error
    const finalStatus = await statusCell.textContent();
    expect(finalStatus).toContain('Idle');

    console.log('✅ Optimistic rollback test passed - status reverted to idle after error');
  });
});
