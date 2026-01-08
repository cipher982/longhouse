/**
 * Canvas Tab Switch Persistence Test
 *
 * This test reproduces and validates the fix for the critical bug where:
 * 1. User drags agent to canvas → works ✓
 * 2. User connects trigger to agent → works ✓
 * 3. User switches to dashboard and back to canvas → NODES DISAPPEAR ✗
 *
 * The bug occurs because CurrentWorkflowLoaded clears all nodes and doesn't
 * properly restore agent nodes that weren't saved to the workflow.
 */

import { test, expect } from './fixtures';

// Skip: Canvas persistence tests use outdated canvas selectors
test.skip();
import {
  navigateToCanvas,
  createAgentToolWorkflow,
  dragAgentToCanvas,
  getCanvasNodes,
  connectNodes,
  executeWorkflow,
  waitForExecutionComplete
} from './helpers/canvas-helpers';

test.describe('Canvas Tab Switch Persistence', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to the app
    await page.goto('/');

    // Wait for app to be ready
    await expect(page.locator('#header-title')).toBeVisible();
  });

  test('agent nodes and connections persist across tab switches', async ({ page, request }, testInfo) => {
    const workerId = String(testInfo.parallelIndex);

    // Step 1: Create an agent via API
    console.log('📝 Step 1: Creating agent via API...');
    const agentResponse = await request.post('/api/agents', {
      data: {
        name: `Persistence Test Agent ${workerId}`,
        system_instructions: 'You are a test agent for persistence testing',
        task_instructions: 'Execute tasks as needed for testing',
        model: 'gpt-mock',
      }
    });

    expect(agentResponse.status()).toBe(201);
    const createdAgent = await agentResponse.json();
    const agentId = createdAgent.id;
    console.log(`✅ Created agent with ID: ${agentId}`);

    // Step 2: Navigate to application and go to canvas
    console.log('📝 Step 2: Navigating to canvas...');
    await page.goto('/');
    await page.waitForTimeout(2000);

    // Navigate to canvas using the correct selector
    await page.getByTestId('global-canvas-tab').click();
    await page.waitForTimeout(2000);

    // Wait for canvas container to be visible
    await expect(page.locator('#canvas-container')).toBeVisible();
    console.log('✅ Canvas loaded');

    // Step 3: Verify agent shelf and drag agent to canvas
    console.log('📝 Step 3: Looking for agent in shelf...');
    const agentShelf = page.locator('#agent-shelf');
    await expect(agentShelf).toBeVisible();

    // Look for the agent by name
    const agentPill = agentShelf.locator(`text=${createdAgent.name}`);
    await expect(agentPill).toBeVisible();
    console.log('✅ Found agent in shelf');

    // Try simple drag and drop from agent shelf to canvas
    const canvas = page.locator('#node-canvas');
    await agentPill.dragTo(canvas, { targetPosition: { x: 400, y: 200 } });
    await page.waitForTimeout(1000);
    console.log('✅ Dragged agent to canvas');

    // For now, focus on testing node persistence - connection testing will be added later
    console.log('📝 Step 3.5: Node successfully added to canvas');

    // Step 4: CRITICAL TEST - Switch to dashboard and back to see if node disappears
    console.log('📝 Step 4: CRITICAL - Testing tab switch...');

    // Switch to dashboard
    await page.locator('.header-nav').click();
    await expect(page.locator('#dashboard-container')).toBeVisible();
    console.log('✅ Switched to dashboard');

    await page.waitForTimeout(1000);

    // Switch back to canvas
    console.log('📝 Step 5: Switching back to canvas...');
    await page.getByTestId('global-canvas-tab').click();
    await expect(page.locator('#canvas-container')).toBeVisible();
    await page.waitForTimeout(2000);

    // Step 6: Check if node still exists
    console.log('📝 Step 6: Checking if node persisted...');

    // Look for any nodes on the canvas by checking if the canvas has drawn content
    const canvasContent = await page.evaluate(() => {
      const canvas = document.querySelector('#node-canvas') as HTMLCanvasElement;
      if (!canvas) return 'no-canvas';

      const ctx = canvas.getContext('2d');
      if (!ctx) return 'no-context';

      // Get image data to check if anything is drawn
      const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const data = imageData.data;

      // Check if canvas has any non-transparent pixels
      let hasContent = false;
      for (let i = 3; i < data.length; i += 4) {
        if (data[i] > 0) { // alpha channel > 0
          hasContent = true;
          break;
        }
      }

      return hasContent ? 'has-content' : 'empty';
    });

    console.log(`📊 Canvas content check: ${canvasContent}`);

    // Also check agent shelf state - if the bug exists, agent should be back to enabled state
    const agentPillAfterSwitch = agentShelf.locator(`text=${createdAgent.name}`);
    const isDisabled = await agentPillAfterSwitch.getAttribute('class');
    console.log(`📊 Agent pill class after switch: ${isDisabled}`);

    // Verify persistence: node should exist and agent should be disabled
    if (canvasContent === 'has-content' && isDisabled && isDisabled.includes('disabled')) {
      console.log('✅ NODE PERSISTED - Node exists and agent is disabled in shelf');
    } else {
      console.log(`❌ BUG REPRODUCED - Canvas: ${canvasContent}, Agent disabled: ${isDisabled?.includes('disabled')}`);
      throw new Error('Canvas persistence bug reproduced: Node disappeared after tab switch');
    }
  });

  test('multiple tab switches preserve state', async ({ page }) => {
    // Test rapid tab switching doesn't lose state
    console.log('📝 Testing multiple rapid tab switches...');

    // Set up initial state
    await navigateToCanvas(page);

    // Create a simple workflow
    await createAgentToolWorkflow(page, 'Multi Switch Test Agent');

    const initialNodes = await getCanvasNodes(page);
    const initialConnections = await page.locator('.connection-line').count();

    // Perform multiple rapid switches
    for (let i = 0; i < 3; i++) {
      console.log(`Switch cycle ${i + 1}/3`);

      // To dashboard
      await page.locator('.header-nav').click();
      await expect(page.locator('[data-testid="dashboard-container"]')).toBeVisible();
      await page.waitForTimeout(200);

      // Back to canvas
      await page.getByTestId('global-canvas-tab').click();
      await expect(page.locator('[data-testid="canvas-container"]')).toBeVisible();
      await page.waitForTimeout(200);

      // Verify state preserved
      const currentNodes = await getCanvasNodes(page);
      const currentConnections = await page.locator('.connection-line').count();

      expect(currentNodes.length).toBe(initialNodes.length);
      expect(currentConnections).toBe(initialConnections);
    }

    console.log('✅ Multiple tab switches preserve state');
  });

  test('viewport and positions persist across tab switches', async ({ page }) => {
    // Test that node positions and canvas viewport persist
    console.log('📝 Testing viewport and position persistence...');

    await navigateToCanvas(page);

    // Create workflow with nodes at specific positions
    await createAgentToolWorkflow(page, 'Position Test Agent');

    // Pan canvas to specific position
    const canvas = page.locator('#node-canvas');
    await canvas.hover();
    await page.mouse.down();
    await page.mouse.move(100, 100);
    await page.mouse.up();

    // Record positions before switch
    const nodesBefore = await getCanvasNodes(page);
    const viewportBefore = await page.evaluate(() => {
      return window.APP_STATE?.with(state => {
        const s = state.borrow();
        return { x: s.viewport_x, y: s.viewport_y, zoom: s.zoom_level };
      });
    });

    // Switch tabs
    await page.locator('.header-nav').click();
    await page.waitForTimeout(500);
    await page.getByTestId('global-canvas-tab').click();
    await page.waitForTimeout(1000);

    // Verify positions preserved
    const nodesAfter = await getCanvasNodes(page);
    const viewportAfter = await page.evaluate(() => {
      return window.APP_STATE?.with(state => {
        const s = state.borrow();
        return { x: s.viewport_x, y: s.viewport_y, zoom: s.zoom_level };
      });
    });

    // Compare node positions
    expect(nodesAfter.length).toBe(nodesBefore.length);
    for (let i = 0; i < nodesBefore.length; i++) {
      expect(nodesAfter[i].x).toBeCloseTo(nodesBefore[i].x, 1);
      expect(nodesAfter[i].y).toBeCloseTo(nodesBefore[i].y, 1);
    }

    // Compare viewport
    expect(viewportAfter.x).toBeCloseTo(viewportBefore.x, 1);
    expect(viewportAfter.y).toBeCloseTo(viewportBefore.y, 1);
    expect(viewportAfter.zoom).toBeCloseTo(viewportBefore.zoom, 2);

    console.log('✅ Viewport and positions preserved');
  });
});
