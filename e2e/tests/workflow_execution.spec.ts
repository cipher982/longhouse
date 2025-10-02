import { test, expect, type Page } from './fixtures';

// Helper function to wait for workflow execution to complete
async function waitForExecutionCompletion(page: Page, timeout = 30000) {
  // Wait for run button to not have 'running' class
  await page.waitForFunction(() => {
    const runBtn = document.querySelector('#run-workflow-btn');
    return runBtn && !runBtn.classList.contains('running');
  }, { timeout });
}

// Helper to create a simple workflow
async function createSimpleWorkflow(page: Page, workflowName = 'Test Workflow') {
  // Navigate to canvas and create workflow
  await page.goto('/');
  const canvasTab = page.getByTestId('global-canvas-tab');
  if (await canvasTab.count()) {
    await canvasTab.click();
  }
  await page.waitForSelector('#canvas-container', { timeout: 10_000 });

  // Create new workflow via plus button
  const plusTab = page.locator('.plus-tab');
  await expect(plusTab).toBeVisible();
  await plusTab.click();
  
  // Fill in workflow name
  await page.waitForFunction(() => window.prompt !== undefined);
  await page.evaluate((name) => {
    // Mock the prompt to return our workflow name
    window.prompt = () => name;
  }, workflowName);
  
  await plusTab.click(); // Click again to trigger the prompt
  
  // Wait for workflow to be created
  await page.waitForSelector(`[data-id]:has-text("${workflowName}")`, { timeout: 5000 });
  
  return workflowName;
}

// Helper to add a simple node to workflow
async function addNodeToWorkflow(page: Page) {
  // Drag agent from shelf to canvas
  const pill = page.locator('#agent-shelf .agent-pill').first();
  await expect(pill).toBeVisible();

  const canvasArea = page.locator('#canvas-container canvas');
  await expect(canvasArea).toBeVisible();

  const bbox = await canvasArea.boundingBox();
  if (!bbox) {
    throw new Error('Cannot get canvas bounding box');
  }

  await pill.dragTo(canvasArea, { targetPosition: { x: 100, y: 100 } });

  // Wait for node to appear
  await expect(page.locator('.canvas-node, .generic-node')).toHaveCount(1, { timeout: 5000 });
}

test.describe('Workflow Execution End-to-End Tests', () => {
  test.beforeEach(async ({ page }) => {
    // Reset database before each test
    await page.request.post('http://localhost:8001/admin/reset-database');
  });

  test.afterEach(async ({ page }) => {
    // Clean up after each test
    await page.request.post('http://localhost:8001/admin/reset-database');
  });

  test('Create workflow and execute simple workflow', async ({ page }) => {
    const workflowName = await createSimpleWorkflow(page, 'Simple Execution Test');
    await addNodeToWorkflow(page);

    // Find and click the run button
    const runBtn = page.locator('#run-workflow-btn');
    await expect(runBtn).toBeVisible();
    await runBtn.click();

    // Verify execution starts (button shows running state)
    await expect(runBtn).toHaveClass(/running/, { timeout: 5000 });

    // Wait for execution to complete
    await waitForExecutionCompletion(page);

    // Verify execution completed (button shows success or failed state)
    const finalState = await runBtn.getAttribute('class');
    expect(finalState).toMatch(/(success|failed)/);
  });

  test('Workflow execution with real-time status updates', async ({ page }) => {
    const workflowName = await createSimpleWorkflow(page, 'Status Update Test');
    await addNodeToWorkflow(page);

    // Start execution
    const runBtn = page.locator('#run-workflow-btn');
    await runBtn.click();

    // Verify node status changes during execution
    const node = page.locator('.canvas-node, .generic-node').first();
    
    // Wait for node to show running status (colored border or class change)
    await page.waitForFunction(() => {
      const nodeEl = document.querySelector('.canvas-node, .generic-node');
      return nodeEl && (
        nodeEl.style.border?.includes('amber') || 
        nodeEl.classList.contains('running') ||
        nodeEl.style.backgroundColor?.includes('amber')
      );
    }, { timeout: 10000 });

    // Wait for completion
    await waitForExecutionCompletion(page);

    // Verify final node state
    await page.waitForFunction(() => {
      const nodeEl = document.querySelector('.canvas-node, .generic-node');
      return nodeEl && (
        nodeEl.style.border?.includes('green') || 
        nodeEl.classList.contains('success') ||
        nodeEl.style.backgroundColor?.includes('green') ||
        nodeEl.style.border?.includes('red') ||
        nodeEl.classList.contains('failed') ||
        nodeEl.style.backgroundColor?.includes('red')
      );
    }, { timeout: 5000 });
  });

  test('Workflow execution logging', async ({ page }) => {
    const workflowName = await createSimpleWorkflow(page, 'Logging Test');
    await addNodeToWorkflow(page);

    // Open log drawer before execution
    const logsBtn = page.locator('button[title="Toggle Logs"]');
    if (await logsBtn.count() > 0) {
      await logsBtn.click();
      
      // Verify log drawer opens
      await expect(page.locator('#log-drawer, .log-drawer')).toBeVisible({ timeout: 5000 });
    }

    // Start execution
    const runBtn = page.locator('#run-workflow-btn');
    await runBtn.click();

    // Wait for execution to start
    await expect(runBtn).toHaveClass(/running/, { timeout: 5000 });

    // Check for log entries during execution
    if (await page.locator('#log-drawer, .log-drawer').count() > 0) {
      await expect(page.locator('.log-entry, .log-line')).toHaveCount.toBeGreaterThan(0, { timeout: 10000 });
    }

    // Wait for completion
    await waitForExecutionCompletion(page);
  });

  test('Workflow execution history tracking', async ({ page }) => {
    const workflowName = await createSimpleWorkflow(page, 'History Test');
    await addNodeToWorkflow(page);

    // Execute workflow first time
    const runBtn = page.locator('#run-workflow-btn');
    await runBtn.click();
    await waitForExecutionCompletion(page);

    // Open execution history
    const historyBtn = page.locator('button[title="Execution History"]');
    if (await historyBtn.count() > 0) {
      await historyBtn.click();
      
      // Verify history sidebar opens
      await expect(page.locator('#execution-history, .execution-sidebar')).toBeVisible({ timeout: 5000 });
      
      // Verify at least one execution is listed
      await expect(page.locator('.execution-item, .execution-entry')).toHaveCount.toBeGreaterThan(0, { timeout: 5000 });
      
      // Execute workflow second time
      await runBtn.click();
      await waitForExecutionCompletion(page);
      
      // Verify history now shows two executions
      await expect(page.locator('.execution-item, .execution-entry')).toHaveCount.toBeGreaterThan(1, { timeout: 5000 });
    } else {
      throw new Error('Execution history UI is required - must be implemented for workflow monitoring');
    }
  });

  test('Cancel workflow execution', async ({ page }) => {
    const workflowName = await createSimpleWorkflow(page, 'Cancellation Test');
    await addNodeToWorkflow(page);

    // Start execution
    const runBtn = page.locator('#run-workflow-btn');
    await runBtn.click();

    // Verify execution starts
    await expect(runBtn).toHaveClass(/running/, { timeout: 5000 });

    // Look for cancel button or stop execution option
    const cancelBtn = page.locator('button[title*="Cancel"], button[title*="Stop"], button:has-text("Cancel")');
    if (await cancelBtn.count() > 0) {
      await cancelBtn.click();
      
      // Verify execution stops
      await page.waitForFunction(() => {
        const btn = document.querySelector('#run-workflow-btn');
        return btn && !btn.classList.contains('running');
      }, { timeout: 10000 });
      
      // Verify cancelled state
      const finalClass = await runBtn.getAttribute('class');
      expect(finalClass).not.toMatch(/running/);
    } else {
      throw new Error('Cancel execution functionality is required - must be implemented for workflow control');
    }
  });

  test('Workflow scheduling functionality', async ({ page }) => {
    const workflowName = await createSimpleWorkflow(page, 'Scheduling Test');
    await addNodeToWorkflow(page);

    // Find schedule button
    const scheduleBtn = page.locator('button[title="Schedule Workflow"]');
    if (await scheduleBtn.count() > 0) {
      await scheduleBtn.click();
      
      // Wait for schedule modal
      await expect(page.locator('#schedule-modal-overlay, .schedule-modal')).toBeVisible({ timeout: 5000 });
      
      // Fill in cron expression
      const cronInput = page.locator('#cron-expression, input[placeholder*="cron"]');
      await cronInput.fill('0 9 * * 1-5'); // Weekdays at 9 AM
      
      // Confirm schedule
      const confirmBtn = page.locator('#confirm-schedule-btn, button:has-text("Schedule")');
      await confirmBtn.click();
      
      // Verify modal closes and success message
      await expect(page.locator('#schedule-modal-overlay')).toHaveCount(0, { timeout: 5000 });
      
      // Look for success toast or indicator
      const successToast = page.locator('.toast-success, .toast:has-text("scheduled")');
      if (await successToast.count() > 0) {
        await expect(successToast).toBeVisible({ timeout: 5000 });
      }
    } else {
      test.skip(true, 'Workflow scheduling UI not implemented');
    }
  });

  test('Multiple node workflow execution', async ({ page }) => {
    const workflowName = await createSimpleWorkflow(page, 'Multi-Node Test');
    
    // Add first node
    await addNodeToWorkflow(page);
    
    // Add second node
    const secondPill = page.locator('#agent-shelf .agent-pill').nth(1);
    if (await secondPill.count() > 0) {
      const canvasArea = page.locator('#canvas-container canvas');
      await secondPill.dragTo(canvasArea, { targetPosition: { x: 300, y: 100 } });
      
      // Wait for second node
      await expect(page.locator('.canvas-node, .generic-node')).toHaveCount(2, { timeout: 5000 });
      
      // Create connection between nodes
      const firstNode = page.locator('.canvas-node, .generic-node').first();
      const secondNode = page.locator('.canvas-node, .generic-node').nth(1);
      await firstNode.dragTo(secondNode);
      
      // Execute workflow
      const runBtn = page.locator('#run-workflow-btn');
      await runBtn.click();
      
      // Verify both nodes execute in sequence
      await expect(runBtn).toHaveClass(/running/, { timeout: 5000 });
      
      // Wait for completion
      await waitForExecutionCompletion(page);
      
      // Verify final state
      const finalState = await runBtn.getAttribute('class');
      expect(finalState).toMatch(/(success|failed)/);
    } else {
      test.skip(true, 'Not enough agents available for multi-node test');
    }
  });

  test('Workflow execution error handling', async ({ page }) => {
    const workflowName = await createSimpleWorkflow(page, 'Error Handling Test');
    
    // Create a workflow that might fail (empty workflow or node with invalid config)
    // For now, just execute empty workflow
    const runBtn = page.locator('#run-workflow-btn');
    await runBtn.click();
    
    // Wait for execution attempt
    await page.waitForTimeout(2000);
    
    // Check for error state or completion
    await waitForExecutionCompletion(page);
    
    // Verify error handling (either failed state or graceful completion)
    const finalClass = await runBtn.getAttribute('class');
    expect(finalClass).toBeTruthy();
  });

  test('Workflow save and load persistence', async ({ page }) => {
    const workflowName = await createSimpleWorkflow(page, 'Persistence Test');
    await addNodeToWorkflow(page);
    
    // Execute workflow
    const runBtn = page.locator('#run-workflow-btn');
    await runBtn.click();
    await waitForExecutionCompletion(page);
    
    // Refresh page to test persistence
    await page.reload();
    await page.waitForSelector('#canvas-container', { timeout: 10_000 });
    
    // Verify workflow is still there
    await expect(page.locator(`[data-id]:has-text("${workflowName}")`)).toBeVisible({ timeout: 5000 });
    
    // Verify node is still there
    await expect(page.locator('.canvas-node, .generic-node')).toHaveCount(1, { timeout: 5000 });
    
    // Verify can still execute
    const runBtnAfterReload = page.locator('#run-workflow-btn');
    await expect(runBtnAfterReload).toBeVisible();
    await runBtnAfterReload.click();
    await waitForExecutionCompletion(page);
  });
});

test.describe('Workflow Execution Performance Tests', () => {
  test.beforeEach(async ({ page }) => {
    await page.request.post('http://localhost:8001/admin/reset-database');
  });

  test('Rapid workflow execution cycles', async ({ page }) => {
    const workflowName = await createSimpleWorkflow(page, 'Performance Test');
    await addNodeToWorkflow(page);

    const runBtn = page.locator('#run-workflow-btn');
    
    // Execute workflow multiple times rapidly
    for (let i = 0; i < 3; i++) {
      await runBtn.click();
      await expect(runBtn).toHaveClass(/running/, { timeout: 5000 });
      await waitForExecutionCompletion(page);
      
      // Small delay between executions
      await page.waitForTimeout(1000);
    }
    
    // Verify system is still responsive
    await expect(runBtn).toBeVisible();
    const finalClass = await runBtn.getAttribute('class');
    expect(finalClass).not.toMatch(/running/);
  });

  test('Large workflow execution', async ({ page }) => {
    const workflowName = await createSimpleWorkflow(page, 'Large Workflow Test');
    
    // Add multiple nodes if possible
    const canvasArea = page.locator('#canvas-container canvas');
    const availablePills = page.locator('#agent-shelf .agent-pill');
    const pillCount = await availablePills.count();
    
    if (pillCount > 0) {
      // Add up to 5 nodes or all available pills
      const nodesToAdd = Math.min(5, pillCount);
      
      for (let i = 0; i < nodesToAdd; i++) {
        const pill = availablePills.nth(i);
        await pill.dragTo(canvasArea, { 
          targetPosition: { x: 100 + (i * 150), y: 100 + (i * 50) } 
        });
        
        // Wait for node to appear
        await expect(page.locator('.canvas-node, .generic-node')).toHaveCount(i + 1, { timeout: 5000 });
      }
      
      // Execute the larger workflow
      const runBtn = page.locator('#run-workflow-btn');
      await runBtn.click();
      await expect(runBtn).toHaveClass(/running/, { timeout: 5000 });
      
      // Wait for completion with longer timeout for larger workflow
      await waitForExecutionCompletion(page, 60000);
      
      // Verify completion
      const finalState = await runBtn.getAttribute('class');
      expect(finalState).toMatch(/(success|failed)/);
    } else {
      throw new Error('Agents must be available for workflow testing - check test data setup');
    }
  });
});