/**
 * E2E Test: Course Button Real-time Updates
 *
 * Validates that clicking the run button results in immediate feedback
 * via optimistic updates, with WebSocket providing authoritative status
 * confirmation and real-time multi-user synchronization.
 *
 * This tests the hybrid optimistic + WebSocket approach.
 */

import { test, expect } from './fixtures';
import { resetDatabase } from './test-utils';

test.describe('Course Button Real-time Update', () => {
  // Reset database before each test
  // Uses strict reset that throws on failure to fail fast
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('should transition to running via optimistic update and websocket', async ({ page }) => {
    await page.goto('/dashboard');

    const createBtn = page.locator('[data-testid="create-fiche-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 10000 });

    // Create fiche with deterministic wait for API response
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/fiches') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    // Wait for the new fiche row to appear
    const ficheRow = page.locator('tr[data-fiche-id]').last();
    await expect(ficheRow).toBeVisible({ timeout: 5000 });

    const ficheId = await ficheRow.getAttribute('data-fiche-id');
    expect(ficheId).toBeTruthy();

    // Get initial status
    const statusCell = ficheRow.locator('td[data-label="Status"]');
    await expect(statusCell).toContainText('Idle', { timeout: 5000 });

    // Find and click the run button
    const runButton = page.locator(`[data-testid="run-fiche-${ficheId}"]`);
    await expect(runButton).toBeVisible({ timeout: 5000 });
    await runButton.click();

    // Optimistic update should be immediate, WebSocket confirms shortly after.
    // Use reasonable timeout to handle backend load variance in parallel test runs.
    await expect(statusCell).toHaveText(/Running/, { timeout: 10000 });

    // Verify the run button is disabled during the run
    await expect(runButton).toBeDisabled({ timeout: 5000 });
  });

  test('should handle run button click with multiple fiches', async ({ page }) => {
    await page.goto('/dashboard');

    const createBtn = page.locator('[data-testid="create-fiche-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 10000 });

    // Create first fiche with deterministic wait
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/fiches') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    // Create second fiche with deterministic wait
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/fiches') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    // Wait for both fiches to appear
    const ficheRows = page.locator('tr[data-fiche-id]');
    await expect.poll(
      async () => await ficheRows.count(),
      { timeout: 10000 }
    ).toBeGreaterThanOrEqual(2);

    // Get the two most recent fiches (last two rows)
    const firstFicheRow = ficheRows.nth(-2);
    const secondFicheRow = ficheRows.last();

    const firstFicheId = await firstFicheRow.getAttribute('data-fiche-id');
    const secondFicheId = await secondFicheRow.getAttribute('data-fiche-id');

    // Get both status cells
    const firstStatusCell = firstFicheRow.locator('td[data-label="Status"]');
    const secondStatusCell = secondFicheRow.locator('td[data-label="Status"]');

    // Both should start as Idle
    await expect(firstStatusCell).toContainText('Idle', { timeout: 5000 });
    await expect(secondStatusCell).toContainText('Idle', { timeout: 5000 });

    // Click run on the first fiche
    const firstRunButton = page.locator(`[data-testid="run-fiche-${firstFicheId}"]`);
    await expect(firstRunButton).toBeVisible({ timeout: 5000 });
    await firstRunButton.click();

    // Verify ONLY the first fiche's status changes
    await expect(firstStatusCell).toHaveText(/Running/, { timeout: 10000 });
    await expect(secondStatusCell).toContainText('Idle', { timeout: 5000 });

    // Now click run on the second fiche
    const secondRunButton = page.locator(`[data-testid="run-fiche-${secondFicheId}"]`);
    await expect(secondRunButton).toBeVisible({ timeout: 5000 });
    await secondRunButton.click();

    // Verify the second fiche's status also changes
    await expect(secondStatusCell).toHaveText(/Running/, { timeout: 10000 });
  });

  test('should rollback optimistic update when API call fails', async ({ page }) => {
    await page.goto('/dashboard');

    const createBtn = page.locator('[data-testid="create-fiche-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 10000 });

    // Create fiche with deterministic wait
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/fiches') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    // Wait for the new fiche row
    const ficheRow = page.locator('tr[data-fiche-id]').last();
    await expect(ficheRow).toBeVisible({ timeout: 5000 });

    const ficheId = await ficheRow.getAttribute('data-fiche-id');
    const statusCell = ficheRow.locator('td[data-label="Status"]');

    // Verify initial status
    await expect(statusCell).toContainText('Idle', { timeout: 5000 });

    // Find the run button
    const runButton = page.locator(`[data-testid="run-fiche-${ficheId}"]`);
    await expect(runButton).toBeVisible({ timeout: 5000 });

    // Mock the API to fail (and assert the route is actually hit to avoid false positives)
    let taskRequests = 0;
    await page.route('**/api/fiches/*/task', async (route) => {
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
