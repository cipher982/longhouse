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
  // Reset database before each test
  test.beforeEach(async ({ request }) => {
    await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
  });

  test('should transition to running via optimistic update and websocket', async ({ page }) => {
    await page.goto('/dashboard');

    const createBtn = page.locator('[data-testid="create-agent-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 10000 });

    // Create agent with deterministic wait for API response
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/agents') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    // Wait for the new agent row to appear
    const agentRow = page.locator('tr[data-agent-id]').last();
    await expect(agentRow).toBeVisible({ timeout: 5000 });

    const agentId = await agentRow.getAttribute('data-agent-id');
    expect(agentId).toBeTruthy();

    // Get initial status
    const statusCell = agentRow.locator('td[data-label="Status"]');
    await expect(statusCell).toContainText('Idle', { timeout: 5000 });

    // Find and click the run button
    const runButton = page.locator(`[data-testid="run-agent-${agentId}"]`);
    await expect(runButton).toBeVisible({ timeout: 5000 });
    await runButton.click();

    // Optimistic update should be immediate, WebSocket confirms shortly after.
    // Use reasonable timeout to handle backend load variance in parallel test runs.
    await expect(statusCell).toHaveText(/Running/, { timeout: 10000 });

    // Verify the run button is disabled during the run
    await expect(runButton).toBeDisabled({ timeout: 5000 });
  });

  test('should handle run button click with multiple agents', async ({ page }) => {
    await page.goto('/dashboard');

    const createBtn = page.locator('[data-testid="create-agent-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 10000 });

    // Create first agent with deterministic wait
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/agents') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    // Create second agent with deterministic wait
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/agents') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    // Wait for both agents to appear
    const agentRows = page.locator('tr[data-agent-id]');
    await expect.poll(
      async () => await agentRows.count(),
      { timeout: 10000 }
    ).toBeGreaterThanOrEqual(2);

    // Get the two most recent agents (last two rows)
    const firstAgentRow = agentRows.nth(-2);
    const secondAgentRow = agentRows.last();

    const firstAgentId = await firstAgentRow.getAttribute('data-agent-id');
    const secondAgentId = await secondAgentRow.getAttribute('data-agent-id');

    // Get both status cells
    const firstStatusCell = firstAgentRow.locator('td[data-label="Status"]');
    const secondStatusCell = secondAgentRow.locator('td[data-label="Status"]');

    // Both should start as Idle
    await expect(firstStatusCell).toContainText('Idle', { timeout: 5000 });
    await expect(secondStatusCell).toContainText('Idle', { timeout: 5000 });

    // Click run on the first agent
    const firstRunButton = page.locator(`[data-testid="run-agent-${firstAgentId}"]`);
    await expect(firstRunButton).toBeVisible({ timeout: 5000 });
    await firstRunButton.click();

    // Verify ONLY the first agent's status changes
    await expect(firstStatusCell).toHaveText(/Running/, { timeout: 10000 });
    await expect(secondStatusCell).toContainText('Idle', { timeout: 5000 });

    // Now click run on the second agent
    const secondRunButton = page.locator(`[data-testid="run-agent-${secondAgentId}"]`);
    await expect(secondRunButton).toBeVisible({ timeout: 5000 });
    await secondRunButton.click();

    // Verify the second agent's status also changes
    await expect(secondStatusCell).toHaveText(/Running/, { timeout: 10000 });
  });

  test('should rollback optimistic update when API call fails', async ({ page }) => {
    await page.goto('/dashboard');

    const createBtn = page.locator('[data-testid="create-agent-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 10000 });

    // Create agent with deterministic wait
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/agents') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    // Wait for the new agent row
    const agentRow = page.locator('tr[data-agent-id]').last();
    await expect(agentRow).toBeVisible({ timeout: 5000 });

    const agentId = await agentRow.getAttribute('data-agent-id');
    const statusCell = agentRow.locator('td[data-label="Status"]');

    // Verify initial status
    await expect(statusCell).toContainText('Idle', { timeout: 5000 });

    // Find the run button
    const runButton = page.locator(`[data-testid="run-agent-${agentId}"]`);
    await expect(runButton).toBeVisible({ timeout: 5000 });

    // Mock the API to fail (and assert the route is actually hit to avoid false positives)
    let taskRequests = 0;
    await page.route('**/api/agents/*/task', async (route) => {
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
