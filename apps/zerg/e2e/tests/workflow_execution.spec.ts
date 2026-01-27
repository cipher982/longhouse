import { test, expect, type Page } from './fixtures';

// Helper function to wait for workflow execution to complete
async function waitForExecutionCompletion(page: Page, timeout = 30000) {
  const statusIndicator = page.locator('.execution-status');
  const hasStatus = await statusIndicator.isVisible({ timeout: 5000 }).catch(() => false);

  if (hasStatus) {
    const finishedStatus = page.locator('.execution-status--finished, .execution-status--cancelled');
    await expect(finishedStatus).toBeVisible({ timeout });
    return;
  }

  // Fallback: Wait for run button to no longer show loading state
  const runBtn = page.locator('.run-button');
  await expect(runBtn).not.toHaveClass(/loading/, { timeout });
}

// Helper to create a test fiche via API
async function createTestFiche(request: any, commisId: string) {
  const ficheResponse = await request.post('/api/fiches', {
    data: {
      name: `Test Fiche ${commisId}-${Date.now()}`,
      system_instructions: 'You are a test fiche',
      task_instructions: 'Execute tasks as requested',
      model: 'gpt-mock',
    }
  });

  expect(ficheResponse.status()).toBe(201);
  const fiche = await ficheResponse.json();
  return fiche;
}

// Helper to navigate to canvas
async function navigateToCanvas(page: Page) {
  await page.goto('/');

  // Wait for app to be ready
  await page.waitForFunction(() => (window as any).__APP_READY__ === true, { timeout: 15000 });

  // Navigate to canvas tab
  const canvasTab = page.getByTestId('global-canvas-tab');
  await expect(canvasTab).toBeVisible({ timeout: 15000 });
  await canvasTab.click();

  // Wait for canvas container to be visible
  await page.waitForSelector('#canvas-container', { timeout: 10_000 });
  await page.waitForTimeout(1000); // Let React Flow initialize
}

// Helper to add an fiche node to workflow
async function addFicheNodeToWorkflow(page: Page, ficheName: string) {
  // Ensure fiche shelf is visible
  const ficheShelf = page.locator('#fiche-shelf');
  await expect(ficheShelf).toBeVisible();

  // Find the fiche pill
  const fichePill = page.locator('#fiche-shelf .fiche-shelf-item').filter({ hasText: ficheName }).first();
  await expect(fichePill).toBeVisible({ timeout: 10000 });

  // Get canvas container for drop target
  const canvasContainer = page.locator('#canvas-container');
  const canvasBbox = await canvasContainer.boundingBox();

  if (!canvasBbox) {
    throw new Error('Cannot get canvas bounding box');
  }

  // Drag fiche to center of canvas
  await fichePill.dragTo(canvasContainer, {
    targetPosition: { x: canvasBbox.width / 2, y: canvasBbox.height / 2 }
  });

  // Wait for node to appear (React Flow creates nodes with .react-flow__node class)
  await page.waitForSelector('.react-flow__node, .canvas-node, .generic-node', { timeout: 5000 });
}

import { resetDatabase } from './test-utils';

test.describe('Workflow Execution End-to-End Tests', () => {
  // Uses strict reset that throws on failure to fail fast
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test.afterEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('Create workflow and execute simple workflow', async ({ page, request }, testInfo) => {
    const commisId = String(testInfo.parallelIndex);

    // Create test fiche via API
    const fiche = await createTestFiche(request, commisId);

    // Navigate to canvas
    await navigateToCanvas(page);

    // Add fiche node to canvas
    await addFicheNodeToWorkflow(page, fiche.name);

    // Find and click the run button
    const runBtn = page.locator('.run-button');
    await expect(runBtn).toBeVisible({ timeout: 10000 });
    await runBtn.click();

    // Verify execution starts (button shows loading state)
    await expect(runBtn).toHaveClass(/loading/, { timeout: 5000 });

    // Wait for execution to complete
    await waitForExecutionCompletion(page);

    // Verify execution completed
    await expect(runBtn).not.toHaveClass(/loading/);
  });

  test('Workflow execution with real-time log streaming', async ({ page, request }, testInfo) => {
    const commisId = String(testInfo.parallelIndex);

    // Create test fiche via API
    const fiche = await createTestFiche(request, commisId);

    // Navigate to canvas
    await navigateToCanvas(page);

    // Add fiche node to canvas
    await addFicheNodeToWorkflow(page, fiche.name);

    // Find and click the run button
    const runBtn = page.locator('.run-button');
    await expect(runBtn).toBeVisible({ timeout: 10000 });

    // Click run button to start execution
    await runBtn.click();

    // Verify execution starts (button shows loading state)
    await expect(runBtn).toHaveClass(/loading/, { timeout: 5000 });

    // Logs panel should auto-open on execution start
    const logsDrawer = page.locator('#execution-logs-drawer');
    await expect(logsDrawer).toBeVisible({ timeout: 10000 });

    // Wait for at least one log entry to appear
    const logEntries = logsDrawer.locator('.log-entry');
    await expect
      .poll(async () => logEntries.count(), {
        timeout: 20000,
        intervals: [500, 1000, 2000],
      })
      .toBeGreaterThan(0);

    // Wait for execution to complete
    await waitForExecutionCompletion(page, 60000);

    // Verify execution completed
    await expect(runBtn).not.toHaveClass(/loading/);
  });

  test('Workflow execution logs panel can be toggled', async ({ page, request }, testInfo) => {
    const commisId = String(testInfo.parallelIndex);

    // Create test fiche via API
    const fiche = await createTestFiche(request, commisId);

    // Navigate to canvas
    await navigateToCanvas(page);

    // Add fiche node to canvas
    await addFicheNodeToWorkflow(page, fiche.name);

    // Find and click the run button
    const runBtn = page.locator('.run-button');
    await expect(runBtn).toBeVisible({ timeout: 10000 });
    await runBtn.click();

    // Wait for logs panel to auto-open
    const logsDrawer = page.locator('#execution-logs-drawer');
    await expect(logsDrawer).toBeVisible({ timeout: 10000 });

    // Find and click the logs button to toggle
    const logsButton = page.locator('.logs-button');
    await expect(logsButton).toBeVisible();
    await logsButton.click();

    // Verify logs panel closes
    await expect(logsDrawer).not.toBeVisible({ timeout: 2000 });

    // Click logs button again to re-open
    await logsButton.click();

    // Verify logs panel re-opens
    await expect(logsDrawer).toBeVisible({ timeout: 2000 });
  });

  test('Workflow execution status indicator updates correctly', async ({ page, request }, testInfo) => {
    const commisId = String(testInfo.parallelIndex);

    // Create test fiche via API
    const fiche = await createTestFiche(request, commisId);

    // Navigate to canvas
    await navigateToCanvas(page);

    // Add fiche node to canvas
    await addFicheNodeToWorkflow(page, fiche.name);

    // Find and click the run button
    const runBtn = page.locator('.run-button');
    await expect(runBtn).toBeVisible({ timeout: 10000 });
    await runBtn.click();

    // Wait for execution status to appear
    const executionStatus = page.locator('.execution-status');
    await expect(executionStatus).toBeVisible({ timeout: 10000 });
    await expect(executionStatus).toHaveClass(/execution-status--running/);

    // Wait for execution to complete
    await waitForExecutionCompletion(page, 60000);

    // Verify execution status transitions to finished
    await expect
      .poll(async () => executionStatus.getAttribute('class'), {
        timeout: 10000,
        intervals: [500, 1000, 2000],
      })
      .toContain('execution-status--finished');

    const phaseLabel = executionStatus.locator('.execution-phase');
    await expect(phaseLabel).toHaveText(/Finished/i);
  });

  test('Workflow save and load persistence', async ({ page, request }, testInfo) => {
    const commisId = String(testInfo.parallelIndex);

    // Create test fiche via API
    const fiche = await createTestFiche(request, commisId);

    // Navigate to canvas
    await navigateToCanvas(page);

    // Add fiche node to canvas
    await addFicheNodeToWorkflow(page, fiche.name);

    // Wait for auto-save (debounced 1 second according to CanvasPage.tsx line 890)
    await page.waitForTimeout(2000);

    // Verify node is on canvas (React Flow nodes only, not compatibility classes)
    const nodeCount = await page.locator('.react-flow__node').count();
    expect(nodeCount).toBeGreaterThan(0);
    const initialNodeCount = nodeCount;

    // Refresh page to test persistence
    await page.reload();
    await page.waitForLoadState('networkidle');

    // Navigate back to canvas
    await navigateToCanvas(page);

    // Verify node is still there after reload
    await page.waitForSelector('.react-flow__node', { timeout: 10000 });
    const nodeCountAfterReload = await page.locator('.react-flow__node').count();
    expect(nodeCountAfterReload).toBe(initialNodeCount);
  });
});
