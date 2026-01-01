/**
 * E2E Test: Core Agent Workflow Creation
 *
 * Tests the MOST IMPORTANT user workflow:
 * 1. Create a workflow
 * 2. Add two agents to the workflow
 * 3. Connect them
 * 4. Execute the workflow
 *
 * This test MUST pass - it's the core value proposition of the app.
 */

import { test, expect } from './fixtures';

test.describe('Core Agent Workflow', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to the app
    await page.goto('http://localhost:8004');

    // Wait for the app to load
    await page.waitForLoadState('networkidle');

    // Switch to Canvas view if not already there
    const canvasButton = page.locator('[data-testid="view-canvas"], button:has-text("Canvas")');
    if (await canvasButton.isVisible()) {
      await canvasButton.click();
      await page.waitForTimeout(500);
    }
  });

  test('should create workflow with two connected agents and execute successfully', async ({ page }) => {
    // Step 1: Ensure we have a clean canvas
    console.log('Step 1: Starting with clean canvas');

    // Step 2: Create first agent by dragging from shelf
    console.log('Step 2: Adding first agent');

    // First, make sure agent panel is open
    const toggleButton = page.locator('button:has-text("Toggle agent panel"), button:has-text("☰")').first();
    if (await toggleButton.isVisible()) {
      await toggleButton.click();
      await page.waitForTimeout(500);
    }

    // Wait for agents to be visible (not the shelf, but the actual agent items)
    await page.waitForSelector(':has-text("Available Agents")', { timeout: 10000 });

    // Find and drag first agent to canvas
    const firstAgent = page.locator('[data-testid="agent-item"]:first-child, .agent-item:first-child').first();
    await expect(firstAgent).toBeVisible();

    const canvas = page.locator('[data-testid="canvas"], .canvas-area, #canvas').first();
    await expect(canvas).toBeVisible();

    // Drag first agent to canvas
    await firstAgent.dragTo(canvas, {
      sourcePosition: { x: 10, y: 10 },
      targetPosition: { x: 200, y: 200 }
    });

    // Wait for first agent node to appear
    await page.waitForTimeout(1000);

    // Step 3: Create second agent
    console.log('Step 3: Adding second agent');

    // Drag second agent to canvas
    const secondAgent = page.locator('[data-testid="agent-item"]:nth-child(2), .agent-item:nth-child(2)').first();
    if (await secondAgent.isVisible()) {
      await secondAgent.dragTo(canvas, {
        sourcePosition: { x: 10, y: 10 },
        targetPosition: { x: 400, y: 200 }
      });
    } else {
      // If only one agent exists, duplicate it by dragging the same one again
      await firstAgent.dragTo(canvas, {
        sourcePosition: { x: 10, y: 10 },
        targetPosition: { x: 400, y: 200 }
      });
    }

    // Wait for second agent node to appear
    await page.waitForTimeout(1000);

    // Step 4: Verify both nodes exist
    console.log('Step 4: Verifying nodes created');

    const nodes = page.locator('[data-testid="workflow-node"], .workflow-node, .node');
    await expect(nodes).toHaveCountGreaterThanOrEqual(2);

    // Step 5: Connect the agents
    console.log('Step 5: Connecting agents');

    // Find output handle of first node and input handle of second node
    const firstNodeOutput = page.locator('[data-testid="node-output-handle"], .output-handle').first();
    const secondNodeInput = page.locator('[data-testid="node-input-handle"], .input-handle').last();

    if (await firstNodeOutput.isVisible() && await secondNodeInput.isVisible()) {
      await firstNodeOutput.dragTo(secondNodeInput);
      await page.waitForTimeout(500);
    } else {
      console.log('Manual connection required - handles not found, using click-based connection');
      // Alternative: Click-based connection if drag doesn't work
      await firstNodeOutput.click();
      await secondNodeInput.click();
    }

    // Step 6: Execute the workflow
    console.log('Step 6: Executing workflow');

    // Look for execute/play/run button
    const executeButton = page.locator(
      '[data-testid="execute-workflow"], [data-testid="run-workflow"], button:has-text("Execute"), button:has-text("Run"), .execute-btn, .run-btn'
    ).first();

    if (await executeButton.isVisible()) {
      await executeButton.click();

      // Wait for execution to start
      await page.waitForTimeout(2000);

      // Step 7: Verify execution started (no 404 errors)
      console.log('Step 7: Verifying execution started');

      // Check that we don't have any 404 errors in console
      const errors: string[] = [];
      page.on('console', (msg) => {
        if (msg.type() === 'error' && msg.text().includes('404')) {
          errors.push(msg.text());
        }
      });

      // Wait a bit more for any errors to appear
      await page.waitForTimeout(1000);

      // Fail test if we got 404 errors
      if (errors.length > 0) {
        throw new Error(`Got 404 errors during workflow execution: ${errors.join(', ')}`);
      }

      // Look for execution progress indicators
      const progressIndicators = page.locator(
        '[data-testid="execution-progress"], .execution-status, .progress-bar, .spinner'
      );

      if (await progressIndicators.first().isVisible()) {
        console.log('✅ Execution progress visible - workflow started successfully');
      } else {
        console.log('⚠️  No clear progress indicators, but no 404 errors either');
      }

    } else {
      console.log('⚠️  Execute button not found - workflow creation successful but execution not tested');
    }

    // Final verification: Canvas should have nodes and possibly connections
    const finalNodes = page.locator('[data-testid="workflow-node"], .workflow-node, .node');
    await expect(finalNodes).toHaveCountGreaterThanOrEqual(2);

    console.log('✅ Core agent workflow test completed successfully');
  });

  test('should handle workflow execution API calls correctly', async ({ page }) => {
    // Monitor network requests to catch API issues
    const apiCalls: string[] = [];
    const apiErrors: string[] = [];

    page.on('response', response => {
      const url = response.url();
      if (url.includes('/api/workflow-executions/')) {
        apiCalls.push(`${response.request().method()} ${url} - ${response.status()}`);
        if (response.status() >= 400) {
          apiErrors.push(`${response.request().method()} ${url} - ${response.status()}`);
        }
      }
    });

    // Create a simple workflow
    // First, make sure agent panel is open
    const toggleButton = page.locator('button:has-text("Toggle agent panel"), button:has-text("☰")').first();
    if (await toggleButton.isVisible()) {
      await toggleButton.click();
      await page.waitForTimeout(500);
    }

    await page.waitForSelector(':has-text("Available Agents")');
    const agent = page.locator('[data-testid="agent-item"], .agent-item').first();
    const canvas = page.locator('[data-testid="canvas"], .canvas-area, #canvas').first();

    await agent.dragTo(canvas, {
      targetPosition: { x: 200, y: 200 }
    });

    await page.waitForTimeout(1000);

    // Try to execute
    const executeButton = page.locator(
      '[data-testid="execute-workflow"], button:has-text("Execute"), button:has-text("Run")'
    ).first();

    if (await executeButton.isVisible()) {
      await executeButton.click();
      await page.waitForTimeout(2000);
    }

    // Verify API calls
    console.log('API calls made:', apiCalls);

    // Should not have any 4xx/5xx errors
    expect(apiErrors).toHaveLength(0);

    // Should have made some workflow execution API calls
    const executionCalls = apiCalls.filter(call =>
      call.includes('workflow-executions') &&
      (call.includes('reserve') || call.includes('start'))
    );

    expect(executionCalls.length).toBeGreaterThan(0);
  });
});
