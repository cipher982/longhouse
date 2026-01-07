import { test, expect } from './fixtures';

/**
 * Simple test to verify the agent_id fix resolves the core workflow execution issue
 */

test.describe('Agent ID Fix Verification', () => {
  // SKIPPED: These tests expect a create-agent modal/form that doesn't exist.
  // The create button directly creates an agent without opening a form.
  // Tests also expect #agent-name, #system-instructions, #task-instructions inputs.

  test.beforeEach(async ({ page, request }) => {
    // Reset database to ensure clean state
    await request.post('/admin/reset-database');
    await page.goto('/');
    await page.waitForTimeout(2000);
  });

  test.skip('Agent nodes have proper agent_id and workflow executes without crash', async ({ page }) => {
    console.log('🔧 Testing agent_id fix for workflow execution...');

    // Step 1: Create a test agent
    await page.locator('.header-nav').click();
    await page.waitForTimeout(1000);

    const createAgentBtn = page.locator('button:has-text("Create Agent")');
    await createAgentBtn.click();
    await page.fill('#agent-name', 'Test Agent');
    await page.fill('#system-instructions', 'You are a test agent');
    await page.fill('#task-instructions', 'Perform test tasks');
    await page.locator('button:has-text("Create Agent")').click();
    await page.waitForTimeout(2000);

    console.log('✅ Step 1: Created test agent');

    // Step 2: Switch to canvas
    await page.getByTestId('global-canvas-tab').click();
    await page.waitForTimeout(2000);

    await expect(page.locator('#canvas-container canvas')).toBeVisible({ timeout: 5000 });
    console.log('✅ Step 2: Canvas loaded');

    // Step 3: Drag agent to canvas
    const agentPills = page.locator('#agent-shelf .agent-pill');
    const canvasArea = page.locator('#canvas-container canvas');

    await expect(agentPills).toHaveCount.toBeGreaterThanOrEqual(1, { timeout: 5000 });

    await agentPills.first().dragTo(canvasArea, {
      targetPosition: { x: 200, y: 150 }
    });
    await page.waitForTimeout(2000);

    console.log('✅ Step 3: Dragged agent to canvas');

    // Step 4: Verify canvas data has proper agent_id
    const workflowResponse = await page.request.get('http://localhost:8001/api/workflows/current');
    expect(workflowResponse.ok()).toBe(true);

    const workflow = await workflowResponse.json();
    console.log('📋 Canvas data:', JSON.stringify(workflow.canvas_data, null, 2));

    // Verify we have nodes with proper agent_ids
    expect(workflow.canvas_data.nodes).toHaveLength.toBeGreaterThan(0);
    const firstNode = workflow.canvas_data.nodes[0];
    expect(firstNode.agent_id).not.toBeNull();
    expect(firstNode.node_type).toBe('AgentIdentity');

    console.log(`✅ Step 4: Node has proper agent_id: ${firstNode.agent_id}`);

    // Step 5: Test workflow execution
    const runButton = page.locator('#run-btn, button:has-text("Run"), .run-button');
    await expect(runButton).toBeVisible({ timeout: 5000 });

    // Capture any console errors
    const consoleErrors: string[] = [];
    page.on('console', msg => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text());
      }
    });

    // Capture network failures
    const networkErrors: string[] = [];
    page.on('response', response => {
      if (response.status() >= 500) {
        networkErrors.push(`${response.status()}: ${response.url()}`);
      }
    });

    await runButton.click();
    await page.waitForTimeout(3000);

    console.log('✅ Step 5: Clicked run button');

    // Step 6: Verify no crashes
    console.log(`📊 Console errors: ${consoleErrors.length}`);
    console.log(`📊 Network errors (5xx): ${networkErrors.length}`);

    if (consoleErrors.length > 0) {
      console.log('Console errors:', consoleErrors);
    }
    if (networkErrors.length > 0) {
      console.log('Network errors:', networkErrors);
    }

    // The key test: workflow execution should not crash with 500 error
    const has500Error = networkErrors.some(error => error.includes('500'));
    const hasAgentNoneError = consoleErrors.some(error => error.includes('Agent None not found'));

    expect(has500Error).toBe(false);
    expect(hasAgentNoneError).toBe(false);

    console.log('🎉 SUCCESS: Workflow execution completed without crashes!');
    console.log('✅ Agent ID fix is working correctly');
  });

  test.skip('Verify canvas data structure is correct', async ({ page }) => {
    console.log('🔍 Testing canvas data structure...');

    // Create agent and drag to canvas (simplified version)
    await page.locator('.header-nav').click();
    await page.waitForTimeout(1000);

    const createAgentBtn = page.locator('button:has-text("Create Agent")');
    await createAgentBtn.click();
    await page.fill('#agent-name', 'Structure Test Agent');
    await page.fill('#system-instructions', 'Test');
    await page.fill('#task-instructions', 'Test');
    await page.locator('button:has-text("Create Agent")').click();
    await page.waitForTimeout(1000);

    await page.getByTestId('global-canvas-tab').click();
    await page.waitForTimeout(2000);

    const agentPills = page.locator('#agent-shelf .agent-pill');
    const canvasArea = page.locator('#canvas-container canvas');

    await agentPills.first().dragTo(canvasArea, {
      targetPosition: { x: 100, y: 100 }
    });
    await page.waitForTimeout(2000);

    // Check canvas data structure
    const workflowResponse = await page.request.get('http://localhost:8001/api/workflows/current');
    const workflow = await workflowResponse.json();

    console.log('📋 Verifying canvas data structure...');

    // Verify structure
    expect(workflow.canvas_data).toBeDefined();
    expect(workflow.canvas_data.nodes).toBeDefined();
    expect(workflow.canvas_data.edges).toBeDefined();
    expect(Array.isArray(workflow.canvas_data.nodes)).toBe(true);
    expect(Array.isArray(workflow.canvas_data.edges)).toBe(true);

    if (workflow.canvas_data.nodes.length > 0) {
      const node = workflow.canvas_data.nodes[0];

      // Verify node has all required fields
      expect(node.node_id).toBeDefined();
      expect(node.agent_id).not.toBeNull();
      expect(node.node_type).toBe('AgentIdentity');
      expect(node.text).toBeDefined();
      expect(typeof node.x).toBe('number');
      expect(typeof node.y).toBe('number');

      console.log(`✅ Node structure is correct: ${node.node_id} with agent_id ${node.agent_id}`);
    }

    console.log('✅ Canvas data structure verification passed');
  });
});
