import { test, expect, type Page } from './fixtures';

// Helper function to wait for workflow execution to complete
async function waitForExecutionCompletion(page: Page, timeout = 30000) {
  // Wait for run button to no longer show loading state
  await page.waitForFunction(() => {
    const runBtn = document.querySelector('.run-button');
    return runBtn && !runBtn.classList.contains('loading');
  }, { timeout });
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
  test.beforeEach(async ({ page, backendUrl }) => {
    // Reset database before each test
    await page.request.post(`${backendUrl}/admin/reset-database`);
  });

  test.afterEach(async ({ page, backendUrl }) => {
    // Clean up after each test
    await page.request.post(`${backendUrl}/admin/reset-database`);
  });

  test('Create workflow and execute simple workflow', async ({ page, request }, testInfo) => {
    const workerId = String(testInfo.workerIndex);

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

  test('Workflow execution with real-time log streaming', async ({ page, request }, testInfo) => {
    const workerId = String(testInfo.workerIndex);

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

    // Wait for logs panel to appear automatically
    // According to CanvasPage.tsx line 915, logs auto-open when execution starts
    const logsDrawer = page.locator('#execution-logs-drawer');
    await expect(logsDrawer).toBeVisible({ timeout: 10000 });

    // Verify log stream header is present
    const logStreamHeader = page.locator('.log-stream-header');
    await expect(logStreamHeader).toBeVisible();

    // Verify "EXECUTION STREAM" title
    const logStreamTitle = page.locator('.log-stream-title');
    await expect(logStreamTitle).toHaveText('EXECUTION STREAM');

    // Verify running indicator appears (● live indicator)
    const runningIndicator = page.locator('.log-stream-indicator');
    await expect(runningIndicator).toBeVisible({ timeout: 5000 });

    // Wait for at least one log entry to appear
    const logEntry = page.locator('.log-entry');
    await expect(logEntry.first()).toBeVisible({ timeout: 15000 });

    // Verify "EXECUTION STARTED" log appears
    const executionStartedLog = page.locator('.log-entry:has-text("EXECUTION STARTED")');
    await expect(executionStartedLog).toBeVisible({ timeout: 5000 });

    // Wait for execution to complete
    await waitForExecutionCompletion(page, 60000);

    // Verify "EXECUTION FINISHED" or similar completion log appears
    const completionLog = page.locator('.log-entry').filter({
      hasText: /EXECUTION (FINISHED|COMPLETED|SUCCESS|FAILED)/i
    });
    await expect(completionLog.first()).toBeVisible({ timeout: 10000 });

    // Verify multiple log entries were created (streaming happened)
    const allLogEntries = await page.locator('.log-entry').count();
    expect(allLogEntries).toBeGreaterThan(1);

    // Verify execution completed
    await expect(runBtn).not.toHaveClass(/loading/);
  });

  test('Workflow execution logs panel can be toggled', async ({ page, request }, testInfo) => {
    const workerId = String(testInfo.workerIndex);

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

  test('Workflow execution status indicator updates correctly', async ({ page, request }, testInfo) => {
    const workerId = String(testInfo.workerIndex);

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
    await expect(executionStatus).toBeVisible({ timeout: 10000 });

    // Verify "Running" phase appears
    const runningPhase = page.locator('.execution-phase:has-text("Running")');
    await expect(runningPhase).toBeVisible({ timeout: 5000 });

    // Wait for execution to complete
    await waitForExecutionCompletion(page, 60000);

    // Verify completion phase appears (Finished, Cancelled, etc.)
    const completionPhase = page.locator('.execution-phase').filter({
      hasText: /Finished|Cancelled|Failed/i
    });
    await expect(completionPhase.first()).toBeVisible({ timeout: 10000 });
  });

  test('Workflow save and load persistence', async ({ page, request }, testInfo) => {
    const workerId = String(testInfo.workerIndex);

    // Create test agent via API
    const agent = await createTestAgent(request, workerId);

    // Navigate to canvas
    await navigateToCanvas(page);

    // Add agent node to canvas
    await addAgentNodeToWorkflow(page, agent.name);

    // Wait for auto-save (debounced 1 second according to CanvasPage.tsx line 890)
    await page.waitForTimeout(2000);

    // Verify node is on canvas
    const nodeCount = await page.locator('.react-flow__node, .canvas-node, .generic-node').count();
    expect(nodeCount).toBe(1);

    // Refresh page to test persistence
    await page.reload();
    await page.waitForLoadState('networkidle');

    // Navigate back to canvas
    await navigateToCanvas(page);

    // Verify node is still there after reload
    await page.waitForSelector('.react-flow__node, .canvas-node, .generic-node', { timeout: 10000 });
    const nodeCountAfterReload = await page.locator('.react-flow__node, .canvas-node, .generic-node').count();
    expect(nodeCountAfterReload).toBe(1);
  });
});
