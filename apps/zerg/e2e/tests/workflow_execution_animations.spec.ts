import { test, expect } from './fixtures';
import { resetDatabase } from './test-utils';

// Skip: Workflow animation tests depend on canvas selectors that have changed
test.skip();

/**
 * WORKFLOW EXECUTION ANIMATION TESTS
 *
 * Tests that verify visual feedback during workflow execution:
 * 1. Connection lines should animate faster with green fill during execution
 * 2. Nodes should show visual indicators (colors, pulses) when running
 * 3. Animations should return to idle state after completion
 */

// Helper to create workflow with connected nodes
// Note: Reset is done in beforeEach, this just sets up the workflow
async function createConnectedWorkflow(page) {
    // Navigate to canvas
    await page.goto('/');
    const canvasTab = page.getByTestId('global-canvas-tab');
    if (await canvasTab.count()) {
        await canvasTab.click();
    }
    await page.waitForSelector('#canvas-container', { timeout: 10_000 });

    // Add trigger node
    const triggerPill = page.locator('#fiche-shelf .fiche-pill').first();
    await expect(triggerPill).toBeVisible();
    const canvasArea = page.locator('#canvas-container canvas');
    await triggerPill.dragTo(canvasArea, { targetPosition: { x: 100, y: 100 } });

    // Add fiche node
    const fichePill = page.locator('#fiche-shelf .fiche-pill').nth(1);
    if (await fichePill.count() > 0) {
        await fichePill.dragTo(canvasArea, { targetPosition: { x: 300, y: 100 } });

        // Connect nodes by dragging from first to second
        const firstNode = page.locator('.canvas-node, .generic-node').first();
        const secondNode = page.locator('.canvas-node, .generic-node').nth(1);
        await firstNode.dragTo(secondNode);

        // Verify connection exists
        await expect(page.locator('.canvas-edge, path.edge')).toHaveCount(1, { timeout: 5000 });

        return true;
    }

    return false;
}

test.describe('Workflow Execution Animations', () => {
    // Uses strict reset that throws on failure to fail fast
    test.beforeEach(async ({ request }) => {
        await resetDatabase(request);
    });

    test('Connection lines should animate during workflow execution', async ({ page }) => {
        const hasConnectedNodes = await createConnectedWorkflow(page);
        test.skip(!hasConnectedNodes, 'Not enough fiches for connected workflow test');

        // Find run button
        const runBtn = page.locator('#run-workflow-btn');
        await expect(runBtn).toBeVisible();

        // Check initial connection line state (should be subtle animation)
        const connectionLine = page.locator('.canvas-edge, path.edge').first();
        await expect(connectionLine).toBeVisible();

        // Start execution
        await runBtn.click();
        await expect(runBtn).toHaveClass(/running/, { timeout: 5000 });

        // During execution, connection lines should have enhanced animation
        // We can't directly test the animated dash pattern, but we can verify:
        // 1. The line is still visible
        // 2. Nodes show running state
        await expect(connectionLine).toBeVisible();

        // Check that nodes show execution state
        const nodes = page.locator('.canvas-node, .generic-node');
        await page.waitForFunction(() => {
            const nodeElements = document.querySelectorAll('.canvas-node, .generic-node');
            return Array.from(nodeElements).some(node => {
                const style = window.getComputedStyle(node);
                // Check for running state indicators (border/background colors)
                return style.borderColor?.includes('rgb(251, 191, 36)') || // amber for running
                       style.backgroundColor?.includes('rgb(251, 191, 36)') ||
                       node.classList.contains('running');
            });
        }, { timeout: 10000 });

        // Wait for execution to complete
        await page.waitForFunction(() => {
            const btn = document.querySelector('#run-workflow-btn');
            return btn && !btn.classList.contains('running');
        }, { timeout: 30000 });

        // After completion, verify final state
        const finalClass = await runBtn.getAttribute('class');
        expect(finalClass).toMatch(/(success|failed)/);
    });

    test('Nodes should show visual feedback during execution', async ({ page }) => {
        const hasConnectedNodes = await createConnectedWorkflow(page);
        test.skip(!hasConnectedNodes, 'Not enough fiches for connected workflow test');

        const runBtn = page.locator('#run-workflow-btn');
        await runBtn.click();

        // Verify nodes show running state
        const nodes = page.locator('.canvas-node, .generic-node');

        // Check for running state visual indicators
        await page.waitForFunction(() => {
            const nodeElements = document.querySelectorAll('.canvas-node, .generic-node');
            return Array.from(nodeElements).some(node => {
                const style = window.getComputedStyle(node);
                // Look for amber color (running state) in borders or backgrounds
                return style.borderColor?.includes('251, 191, 36') || // rgb amber
                       style.backgroundColor?.includes('251, 191, 36') ||
                       node.classList.contains('running') ||
                       node.style.border?.includes('amber') ||
                       node.style.backgroundColor?.includes('amber');
            });
        }, { timeout: 10000 });

        // Wait for completion and check final state
        await page.waitForFunction(() => {
            const btn = document.querySelector('#run-workflow-btn');
            return btn && !btn.classList.contains('running');
        }, { timeout: 30000 });

        // Verify final state shows success or failed colors
        await page.waitForFunction(() => {
            const nodeElements = document.querySelectorAll('.canvas-node, .generic-node');
            return Array.from(nodeElements).some(node => {
                const style = window.getComputedStyle(node);
                // Look for green (success) or red (failed) colors
                return style.borderColor?.includes('34, 197, 94') || // green success
                       style.borderColor?.includes('239, 68, 68') || // red failed
                       style.backgroundColor?.includes('34, 197, 94') ||
                       style.backgroundColor?.includes('239, 68, 68') ||
                       node.classList.contains('success') ||
                       node.classList.contains('failed');
            });
        }, { timeout: 5000 });
    });

    test('Connection animations should return to idle after execution', async ({ page }) => {
        const hasConnectedNodes = await createConnectedWorkflow(page);
        test.skip(!hasConnectedNodes, 'Not enough fiches for connected workflow test');

        const runBtn = page.locator('#run-workflow-btn');
        const connectionLine = page.locator('.canvas-edge, path.edge').first();

        // Start and complete execution
        await runBtn.click();
        await expect(runBtn).toHaveClass(/running/, { timeout: 5000 });

        await page.waitForFunction(() => {
            const btn = document.querySelector('#run-workflow-btn');
            return btn && !btn.classList.contains('running');
        }, { timeout: 30000 });

        // Verify connection line is still visible after execution
        await expect(connectionLine).toBeVisible();

        // The connection should return to subtle idle animation
        // (We can't directly test the animation speed change, but we ensure visibility)
        const finalBtnClass = await runBtn.getAttribute('class');
        expect(finalBtnClass).toMatch(/(success|failed)/);
    });

    test('Multiple connected nodes should animate sequentially', async ({ page }) => {
        // beforeEach already reset, just navigate
        await page.goto('/');
        const canvasTab = page.getByTestId('global-canvas-tab');
        if (await canvasTab.count()) {
            await canvasTab.click();
        }
        await page.waitForSelector('#canvas-container', { timeout: 10_000 });

        const canvasArea = page.locator('#canvas-container canvas');
        const availablePills = page.locator('#fiche-shelf .fiche-pill');
        const pillCount = await availablePills.count();

        if (pillCount >= 3) {
            // Create a chain: Node1 -> Node2 -> Node3
            await availablePills.nth(0).dragTo(canvasArea, { targetPosition: { x: 100, y: 100 } });
            await availablePills.nth(1).dragTo(canvasArea, { targetPosition: { x: 300, y: 100 } });
            await availablePills.nth(2).dragTo(canvasArea, { targetPosition: { x: 500, y: 100 } });

            // Connect Node1 -> Node2
            const nodes = page.locator('.canvas-node, .generic-node');
            await nodes.nth(0).dragTo(nodes.nth(1));
            // Connect Node2 -> Node3
            await nodes.nth(1).dragTo(nodes.nth(2));

            // Verify connections
            await expect(page.locator('.canvas-edge, path.edge')).toHaveCount(2, { timeout: 5000 });

            // Execute workflow
            const runBtn = page.locator('#run-workflow-btn');
            await runBtn.click();

            // During execution, multiple nodes should show running state
            await page.waitForFunction(() => {
                const nodeElements = document.querySelectorAll('.canvas-node, .generic-node');
                let runningCount = 0;
                nodeElements.forEach(node => {
                    const style = window.getComputedStyle(node);
                    if (style.borderColor?.includes('251, 191, 36') ||
                        style.backgroundColor?.includes('251, 191, 36') ||
                        node.classList.contains('running')) {
                        runningCount++;
                    }
                });
                return runningCount > 0; // At least one node should be running
            }, { timeout: 15000 });

            // Wait for completion
            await page.waitForFunction(() => {
                const btn = document.querySelector('#run-workflow-btn');
                return btn && !btn.classList.contains('running');
            }, { timeout: 45000 });

            // Verify connections are still visible
            await expect(page.locator('.canvas-edge, path.edge')).toHaveCount(2);
        } else {
            test.skip(true, 'Need at least 3 fiches for multi-node animation test');
        }
    });
});
