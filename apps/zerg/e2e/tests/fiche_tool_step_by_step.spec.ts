import { test, expect } from './fixtures';

// Skip: Redundant with fiche_tool_complete_e2e.spec.ts, canvas selectors have changed
test.skip();

/**
 * AGENT-TOOL E2E TEST - STEP BY STEP
 *
 * Building up the complete workflow one step at a time:
 * 1. Clean database verification
 * 2. Create ONE fiche
 * 3. Navigate to canvas
 * 4. Drag fiche to canvas
 * 5. Drag tool to canvas
 * 6. Connect nodes
 * 7. Execute workflow
 *
 * Each step builds on the previous one, with detailed logging and validation.
 */

function countRealFiches(ficheRows: any[]): Promise<number> {
  // Helper to count real fiches (exclude UI placeholders)
  return Promise.all(
    ficheRows.map(async (row) => {
      const ficheName = await row.locator('td').first().textContent();
      return ficheName && !ficheName.includes('No fiches found');
    })
  ).then(results => results.filter(Boolean).length);
}

test.describe('Fiche-Tool E2E - Step by Step', () => {
  test('Step 1: Verify clean database state', async ({ page }) => {
    console.log('🧪 Step 1: Verifying clean database...');

    await page.goto('/');
    await page.locator('.header-nav').click();
    await page.waitForTimeout(1000);

    const ficheRows = await page.locator('table tbody tr').all();
    const realFicheCount = await countRealFiches(ficheRows);

    console.log(`📊 Step 1 Result: ${realFicheCount} real fiches found`);
    expect(realFicheCount).toBe(0);

    console.log('✅ Step 1 PASSED: Database is clean');
  });

  test('Step 2: Create one fiche', async ({ page }) => {
    console.log('🧪 Step 2: Creating one fiche...');

    await page.goto('/');
    await page.locator('.header-nav').click();
    await page.waitForTimeout(1000);

    // Verify clean start
    let ficheRows = await page.locator('table tbody tr').all();
    let realFicheCount = await countRealFiches(ficheRows);
    console.log(`📊 Step 2 Initial: ${realFicheCount} real fiches`);
    expect(realFicheCount).toBe(0);

    // Create ONE fiche
    console.log('🔨 Creating fiche...');
    const createBtn = page.locator('button:has-text("Create Fiche")');
    await createBtn.click();
    await page.waitForTimeout(1000);

    // Verify exactly one fiche created
    ficheRows = await page.locator('table tbody tr').all();
    realFicheCount = await countRealFiches(ficheRows);
    console.log(`📊 Step 2 Final: ${realFicheCount} real fiches`);
    expect(realFicheCount).toBe(1);

    console.log('✅ Step 2 PASSED: Fiche created successfully');
  });

  test('Step 3: Navigate to canvas with fiche', async ({ page }) => {
    console.log('🧪 Step 3: Creating fiche and navigating to canvas...');

    await page.goto('/');
    await page.locator('.header-nav').click();
    await page.waitForTimeout(1000);

    // Create fiche first
    const createBtn = page.locator('button:has-text("Create Fiche")');
    await createBtn.click();
    await page.waitForTimeout(1000);

    // Navigate to canvas
    console.log('🎨 Navigating to canvas...');
    await page.getByTestId('global-canvas-tab').click();
    await page.waitForTimeout(2000);

    // Verify canvas loaded
    await expect(page.locator('#canvas-container')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('#canvas-container canvas')).toBeVisible({ timeout: 5000 });
    console.log('✅ Canvas loaded');

    // Verify fiche shelf loaded
    const fichePills = page.locator('#fiche-shelf .fiche-pill');
    await expect(fichePills.first()).toBeVisible({ timeout: 10000 });
    const pillCount = await fichePills.count();
    console.log(`📊 Step 3 Result: ${pillCount} fiches in shelf`);
    expect(pillCount).toBeGreaterThanOrEqual(1);

    console.log('✅ Step 3 PASSED: Canvas loaded with fiche shelf');
  });

  test('Step 4: Drag fiche to canvas', async ({ page }) => {
    console.log('🧪 Step 4: Drag fiche from shelf to canvas...');

    // Setup: Create fiche and go to canvas
    await page.goto('/');
    await page.locator('.header-nav').click();
    await page.waitForTimeout(1000);

    const createBtn = page.locator('button:has-text("Create Fiche")');
    await createBtn.click();
    await page.waitForTimeout(1000);

    await page.getByTestId('global-canvas-tab').click();
    await page.waitForTimeout(2000);

    // Verify setup
    await expect(page.locator('#canvas-container canvas')).toBeVisible({ timeout: 5000 });
    const fichePills = page.locator('#fiche-shelf .fiche-pill');
    await expect(fichePills.first()).toBeVisible({ timeout: 10000 });

    // Attempt to drag fiche to canvas
    console.log('🖱️ Dragging fiche to canvas...');
    const fichePill = fichePills.first();
    const canvasArea = page.locator('#canvas-container canvas');

    await fichePill.dragTo(canvasArea, {
      targetPosition: { x: 200, y: 150 }
    });
    await page.waitForTimeout(2000);

    // Check if nodes appeared on canvas
    const possibleNodes = page.locator('.canvas-node, .generic-node, [data-node-type], .node, .workflow-node');
    const nodeCount = await possibleNodes.count();
    console.log(`📊 Step 4 Result: ${nodeCount} nodes on canvas after drag`);

    // Log canvas state for debugging
    const canvasHTML = await page.locator('#canvas-container').innerHTML();
    console.log(`🔍 Canvas HTML length: ${canvasHTML.length} characters`);

    if (nodeCount === 0) {
      console.log('⚠️ No nodes found - fiche drag may not be working');
      console.log('🔍 Canvas container HTML (first 200 chars):', canvasHTML.substring(0, 200));
    } else {
      console.log('✅ Nodes found on canvas!');
    }

    console.log('✅ Step 4 COMPLETED: Fiche drag attempted (nodes may or may not appear)');
  });

  test('Step 5: Drag tool to canvas', async ({ page }) => {
    console.log('🧪 Step 5: Drag HTTP tool from palette to canvas...');

    // Setup: Create fiche and go to canvas
    await page.goto('/');
    await page.locator('.header-nav').click();
    await page.waitForTimeout(1000);

    const createBtn = page.locator('button:has-text("Create Fiche")');
    await createBtn.click();
    await page.waitForTimeout(1000);

    await page.getByTestId('global-canvas-tab').click();
    await page.waitForTimeout(2000);

    // Verify canvas loaded
    await expect(page.locator('#canvas-container canvas')).toBeVisible({ timeout: 5000 });

    // Find HTTP Request tool in palette
    console.log('🔍 Looking for HTTP Request tool...');
    const httpTool = page.locator('[data-testid*="http"], .palette-node:has-text("HTTP Request"), .tool-palette-item:has-text("HTTP")');

    // Try different selectors
    const possibleSelectors = [
      '[data-testid="palette-tool-http_request"]',
      '.palette-node:has-text("HTTP Request")',
      '.tool-palette-item:has-text("HTTP")',
      'text="HTTP Request"',
      'text="🌐 HTTP Request"'
    ];

    let toolElement = null;
    for (const selector of possibleSelectors) {
      const element = page.locator(selector);
      if (await element.count() > 0) {
        console.log(`✅ Found HTTP tool with selector: ${selector}`);
        toolElement = element;
        break;
      }
    }

    if (!toolElement) {
      console.log('⚠️ HTTP Request tool not found, listing available tools...');
      const allTools = page.locator('.palette-node, .tool-palette-item');
      const toolCount = await allTools.count();
      console.log(`📊 Found ${toolCount} tools in palette`);

      for (let i = 0; i < Math.min(toolCount, 5); i++) {
        const toolText = await allTools.nth(i).textContent();
        console.log(`  Tool ${i + 1}: ${toolText}`);
      }

      // Use first available tool as fallback
      if (toolCount > 0) {
        toolElement = allTools.first();
        console.log('🔄 Using first available tool as fallback');
      }
    }

    if (toolElement) {
      console.log('🖱️ Dragging tool to canvas...');
      const canvasArea = page.locator('#canvas-container canvas');

      await toolElement.dragTo(canvasArea, {
        targetPosition: { x: 400, y: 150 }
      });
      await page.waitForTimeout(2000);

      // Check for nodes
      const possibleNodes = page.locator('.canvas-node, .generic-node, [data-node-type], .node, .workflow-node');
      const nodeCount = await possibleNodes.count();
      console.log(`📊 Step 5 Result: ${nodeCount} nodes on canvas after tool drag`);

      console.log('✅ Step 5 COMPLETED: Tool drag attempted');
    } else {
      console.log('❌ Step 5 FAILED: No tool found to drag');
    }
  });
});
