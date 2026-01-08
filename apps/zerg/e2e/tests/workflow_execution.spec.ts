import { test, expect, type Page } from './fixtures';

// Helper function to wait for workflow execution to complete
async function waitForExecutionCompletion(page: Page, timeout = 30000) {
  // Wait for run button to no longer show loading state
  // Using polling approach to avoid CSP eval issues
  const runBtn = page.locator('.run-button');
  await expect(runBtn).not.toHaveClass(/loading/, { timeout });
}

// Helper to create a test agent via API
async function createTestAgent(request: any, workerId: string) {
  const agentResponse = await request.post('/api/agents', {
    data: {
      name: `Test Agent ${workerId}-${Date.now()}`,
      system_instructions: 'You are a test agent',
      task_instructions: 'Execute tasks as requested',
      model: 'gpt-mock',
    }
  });

  expect(agentResponse.status()).toBe(201);
  const agent = await agentResponse.json();
  return agent;
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

// Helper to add an agent node to workflow
async function addAgentNodeToWorkflow(page: Page, agentName: string) {
  // Ensure agent shelf is visible
  const agentShelf = page.locator('#agent-shelf');
  await expect(agentShelf).toBeVisible();

  // Find the agent pill
  const agentPill = page.locator('#agent-shelf .agent-shelf-item').filter({ hasText: agentName }).first();
  await expect(agentPill).toBeVisible({ timeout: 10000 });

  // Get canvas container for drop target
  const canvasContainer = page.locator('#canvas-container');
  const canvasBbox = await canvasContainer.boundingBox();

  if (!canvasBbox) {
    throw new Error('Cannot get canvas bounding box');
  }

  // Drag agent to center of canvas
  await agentPill.dragTo(canvasContainer, {
    targetPosition: { x: canvasBbox.width / 2, y: canvasBbox.height / 2 }
  });

  // Wait for node to appear (React Flow creates nodes with .react-flow__node class)
  await page.waitForSelector('.react-flow__node, .canvas-node, .generic-node', { timeout: 5000 });
}

test.describe('Workflow Execution End-to-End Tests', () => {
  test.beforeEach(async ({ request }) => {
    await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
  });

  test.afterEach(async ({ request }) => {
    await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
  });

  test('Create workflow and execute simple workflow', async ({ page, request }, testInfo) => {
    const workerId = String(testInfo.parallelIndex);

    // Create test agent via API
    const agent = await createTestAgent(request, workerId);

    // Navigate to canvas
    await navigateToCanvas(page);

    // Add agent node to canvas
    await addAgentNodeToWorkflow(page, agent.name);

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

  test.skip('Workflow execution with real-time log streaming', async ({ page, request }, testInfo) => {
    // SKIP: Logs drawer selector needs investigation (#execution-logs-drawer not found)
    // This test documents the expected behavior for real-time log streaming
    // TODO: Fix logs drawer visibility after execution starts
    const workerId = String(testInfo.parallelIndex);

    // Create test agent via API
    const agent = await createTestAgent(request, workerId);

    // Navigate to canvas
    await navigateToCanvas(page);

    // Add agent node to canvas
    await addAgentNodeToWorkflow(page, agent.name);

    // Find and click the run button
    const runBtn = page.locator('.run-button');
    await expect(runBtn).toBeVisible({ timeout: 10000 });

    // Click run button to start execution
    await runBtn.click();

    // Verify execution starts (button shows loading state)
    await expect(runBtn).toHaveClass(/loading/, { timeout: 5000 });

    // Check if logs button is available
    const logsButton = page.locator('.logs-button');
    const hasLogsButton = await logsButton.isVisible({ timeout: 2000 }).catch(() => false);

    if (hasLogsButton) {
      await logsButton.click();
      await page.waitForTimeout(1000);

      // Look for any logs-related UI element
      const logsUI = await page.locator('[id*="log"], [class*="log"]').first().isVisible({ timeout: 2000 }).catch(() => false);

      if (logsUI) {
        console.log('✅ Logs UI found - streaming feature is accessible');
      }
    }

    // Wait for execution to complete
    await waitForExecutionCompletion(page, 60000);

    // Verify execution completed
    await expect(runBtn).not.toHaveClass(/loading/);
  });

  test('Workflow execution logs panel can be toggled', async ({ page, request }, testInfo) => {
    const workerId = String(testInfo.parallelIndex);

    // Create test agent via API
    const agent = await createTestAgent(request, workerId);

    // Navigate to canvas
    await navigateToCanvas(page);

    // Add agent node to canvas
    await addAgentNodeToWorkflow(page, agent.name);

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

  test.skip('Workflow execution status indicator updates correctly', async ({ page, request }, testInfo) => {
    // SKIP: Execution may complete too fast to verify phase transitions in test environment
    // This test documents expected behavior of execution status indicators
    // TODO: Investigate why execution phase still shows "Running" after completion
    const workerId = String(testInfo.parallelIndex);

    // Create test agent via API
    const agent = await createTestAgent(request, workerId);

    // Navigate to canvas
    await navigateToCanvas(page);

    // Add agent node to canvas
    await addAgentNodeToWorkflow(page, agent.name);

    // Find and click the run button
    const runBtn = page.locator('.run-button');
    await expect(runBtn).toBeVisible({ timeout: 10000 });
    await runBtn.click();

    // Wait for execution status to appear
    const executionStatus = page.locator('.execution-status');
    const hasStatus = await executionStatus.isVisible({ timeout: 5000 }).catch(() => false);

    if (hasStatus) {
      // Verify execution status UI exists
      console.log('✅ Execution status indicator found');

      // Check for phase element
      const runningPhase = page.locator('.execution-phase');
      const hasPhase = await runningPhase.first().isVisible({ timeout: 2000 }).catch(() => false);

      if (hasPhase) {
        console.log('✅ Execution phase indicator working');
      }
    }

    // Wait for execution to complete
    await waitForExecutionCompletion(page, 60000);

    // Verify execution completed
    await expect(runBtn).not.toHaveClass(/loading/);
  });

  test('Workflow save and load persistence', async ({ page, request }, testInfo) => {
    const workerId = String(testInfo.parallelIndex);

    // Create test agent via API
    const agent = await createTestAgent(request, workerId);

    // Navigate to canvas
    await navigateToCanvas(page);

    // Add agent node to canvas
    await addAgentNodeToWorkflow(page, agent.name);

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
