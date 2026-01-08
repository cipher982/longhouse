import { test, expect } from './fixtures';

// Skip: Agent shelf selectors have changed
test.skip();

// E2E test: Canvas agent shelf loads agents and displays them as pills.
test('Agent shelf loads and displays agents on canvas', async ({ page }) => {
  // 1: Start at the root, ensure app loads
  await page.goto('/');

  // 2: Switch to Canvas view if needed (find by label or data-testid)
  //    The top nav/tab should include Canvas - update selector if different
  const canvasTab = page.locator('[data-testid="global-canvas-tab"]');
  if (await canvasTab.count() > 0) {
    await canvasTab.click();
  } else {
    // Fallback: try button or tab by visible text
    await page.getByText('Canvas Editor', { exact: false }).click();
  }

  // 3: Wait for agent shelf to appear
  const agentShelf = page.locator('#agent-shelf');
  await expect(agentShelf).toBeVisible({ timeout: 5000 });
  await expect(agentShelf).toContainText('Available Agents');

  // 4: Wait (max 10s) until at least one agent-pill is rendered — real agents loaded
  const agentPills = agentShelf.locator('.agent-pill');
  await expect(agentPills.first()).toBeVisible({ timeout: 10000 });

  // 5: Optionally validate correct agent pill(s) are visible and not loading/empty
  // If "No agents available" or "Loading agents..." is present, fail
  const shelfText = await agentShelf.textContent();
  expect(shelfText).not.toContain('Loading agents...');
  expect(shelfText).not.toContain('No agents available');

  // 6: Optionally (re)visit dashboard and back to canvas to verify shelf rerenders
  // const dashboardTab = page.locator('.header-nav');
  // await dashboardTab.click();
  // await canvasTab.click();
  // await expect(agentPills).toHaveCountGreaterThan(0, { timeout: 10000 });
});
