import { test, expect, type Page } from './fixtures';

async function switchToCanvas(page: Page) {
  await page.goto('/');
  const canvasTab = page.getByTestId('global-canvas-tab');
  if (await canvasTab.count()) {
    await canvasTab.click();
  }
  await page.waitForSelector('#canvas-container', { timeout: 10_000 });
}

test.describe('Canvas Editor basic node interactions', () => {
  test.beforeEach(async ({ page }) => {
    await switchToCanvas(page);
  });

  test('Drag agent from shelf to canvas', async ({ page }) => {
    const pill = page.locator('#agent-shelf .agent-pill').first();
    await expect(pill).toBeVisible();

    const canvasArea = page.locator('#canvas-container canvas');
    // REQUIRE canvas to exist - core functionality
    await expect(canvasArea).toBeVisible({ timeout: 5000 });
    
    const bbox = await canvasArea.boundingBox();
    expect(bbox).toBeTruthy(); // Canvas must have proper dimensions

    await pill.dragTo(canvasArea, { targetPosition: { x: 50, y: 50 } });

    // A new node should appear (class .canvas-node or similar)
    await expect(page.locator('.canvas-node, .generic-node')).toHaveCount(1, { timeout: 5000 });
  });

  test('Position node at specific coordinates', async ({ page }) => {
    // First ensure we have a node by dragging one from shelf
    const pill = page.locator('#agent-shelf .agent-pill').first();
    await expect(pill).toBeVisible();
    const canvasArea = page.locator('#canvas-container canvas');
    await pill.dragTo(canvasArea, { targetPosition: { x: 50, y: 50 } });
    
    const node = page.locator('.canvas-node, .generic-node').first();
    await expect(node).toBeVisible({ timeout: 5000 });
    const posBefore = await node.boundingBox();
    expect(posBefore).toBeTruthy(); // Node must have position

    await node.dragTo(node, { targetPosition: { x: posBefore.x + 100, y: posBefore.y + 100 } });

    const posAfter = await node.boundingBox();
    if (!posAfter) {
      throw new Error('Node lost after drag operation');
    }
    expect(posAfter.x).toBeGreaterThan(posBefore.x);
  });

  test('Create edge between two nodes', async ({ page }) => {
    // First create two nodes by dragging from shelf
    const pill = page.locator('#agent-shelf .agent-pill').first();
    await expect(pill).toBeVisible();
    const canvasArea = page.locator('#canvas-container canvas');
    
    // Create first node
    await pill.dragTo(canvasArea, { targetPosition: { x: 50, y: 50 } });
    // Create second node  
    await pill.dragTo(canvasArea, { targetPosition: { x: 200, y: 50 } });
    
    const nodes = page.locator('.canvas-node, .generic-node');
    await expect(nodes).toHaveCount(2, { timeout: 5000 }); // REQUIRE two nodes

    const first = nodes.nth(0);
    const second = nodes.nth(1);

    const p1 = await first.boundingBox();
    const p2 = await second.boundingBox();
    expect(p1).toBeTruthy(); // Nodes must have positions
    expect(p2).toBeTruthy();

    await first.dragTo(second);

    // Expect a new edge element – selector is approximate
    await expect(page.locator('.canvas-edge, path.edge')).toHaveCount(1, { timeout: 5000 });
  });

  test('Delete node from canvas', async ({ page }) => {
    // First create a node to delete
    const pill = page.locator('#agent-shelf .agent-pill').first();
    await expect(pill).toBeVisible();
    const canvasArea = page.locator('#canvas-container canvas');
    await pill.dragTo(canvasArea, { targetPosition: { x: 100, y: 100 } });
    
    const node = page.locator('.canvas-node, .generic-node').first();
    await expect(node).toBeVisible({ timeout: 5000 }); // REQUIRE node exists
    await node.click({ button: 'right' });
    const deleteOpt = page.locator('text=Delete Node');
    await expect(deleteOpt).toBeVisible({ timeout: 5000 }); // REQUIRE context menu
    await deleteOpt.click();
    await expect(node).toHaveCount(0);
  });

  test('Delete edge between nodes', async ({ page }) => {
    // First create two connected nodes
    const pill = page.locator('#agent-shelf .agent-pill').first();
    await expect(pill).toBeVisible();
    const canvasArea = page.locator('#canvas-container canvas');
    
    // Create and connect two nodes
    await pill.dragTo(canvasArea, { targetPosition: { x: 50, y: 50 } });
    await pill.dragTo(canvasArea, { targetPosition: { x: 200, y: 50 } });
    
    const nodes = page.locator('.canvas-node, .generic-node');
    await expect(nodes).toHaveCount(2, { timeout: 5000 });
    
    // Create edge by dragging between nodes
    await nodes.nth(0).dragTo(nodes.nth(1));
    await expect(page.locator('.canvas-edge, path.edge')).toHaveCount(1, { timeout: 5000 });
    
    // Right-click on edge to delete it
    const edge = page.locator('.canvas-edge, path.edge').first();
    await edge.click({ button: 'right' });
    const deleteEdgeOption = page.locator('text=Delete Edge, text=Remove Connection');
    await expect(deleteEdgeOption).toBeVisible({ timeout: 5000 });
    await deleteEdgeOption.click();
    
    // Verify edge is deleted
    await expect(page.locator('.canvas-edge, path.edge')).toHaveCount(0, { timeout: 5000 });
  });

  test('Select multiple nodes (placeholder)', async () => {
    test.skip();
  });

  test('Save and load workflow', async ({ page }) => {
    // Create a workflow with a node - REQUIRE agents to exist
    const pill = page.locator('#agent-shelf .agent-pill').first();
    await expect(pill).toBeVisible({ timeout: 5000 }); // REQUIRE agents in shelf

    const canvasArea = page.locator('#canvas-container canvas');
    await pill.dragTo(canvasArea, { targetPosition: { x: 200, y: 200 } });
    
    // Wait for node to appear
    await expect(page.locator('.canvas-node, .generic-node')).toHaveCount(1, { timeout: 5000 });
    
    // Reload page to test persistence
    await page.reload();
    await page.waitForSelector('#canvas-container', { timeout: 10_000 });
    
    // Verify node persists after reload
    await expect(page.locator('.canvas-node, .generic-node')).toHaveCount(1, { timeout: 5000 });
  });

  test('Clear canvas', async ({ page }) => {
    // First ensure we have some nodes - REQUIRE agents to exist
    const pill = page.locator('#agent-shelf .agent-pill').first();
    await expect(pill).toBeVisible({ timeout: 5000 }); // REQUIRE agents in shelf

    const canvasArea = page.locator('#canvas-container canvas');
    await pill.dragTo(canvasArea, { targetPosition: { x: 100, y: 100 } });
    await expect(page.locator('.canvas-node, .generic-node')).toHaveCount(1, { timeout: 5000 });

    // REQUIRE canvas management controls to exist
    const dropdownToggle = page.locator('.dropdown-toggle');
    await expect(dropdownToggle).toBeVisible({ timeout: 5000 });
    await dropdownToggle.click();
    
    const clearOption = page.locator('text=Clear Canvas');
    await expect(clearOption).toBeVisible({ timeout: 5000 }); // REQUIRE clear functionality
    await clearOption.click();
    
    // Verify canvas is cleared
    await expect(page.locator('.canvas-node, .generic-node')).toHaveCount(0, { timeout: 5000 });
  });

  test('Test zoom/pan controls', async ({ page }) => {
    const canvasArea = page.locator('#canvas-container');
    await canvasArea.click();
    await page.keyboard.press('Meta+-');
    await page.keyboard.press('Meta++');
    // No assertion – ensure no errors
  });
});
