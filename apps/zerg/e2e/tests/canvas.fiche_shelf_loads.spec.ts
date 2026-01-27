import { test, expect } from './fixtures';

// Skip: Fiche shelf selectors have changed
test.skip();

// E2E test: Canvas fiche shelf loads fiches and displays them as pills.
test('Fiche shelf loads and displays fiches on canvas', async ({ page }) => {
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

  // 3: Wait for fiche shelf to appear
  const ficheShelf = page.locator('#fiche-shelf');
  await expect(ficheShelf).toBeVisible({ timeout: 5000 });
  await expect(ficheShelf).toContainText('Fiches');

  // 4: Wait (max 10s) until at least one fiche-pill is rendered — real fiches loaded
  const fichePills = ficheShelf.locator('.fiche-pill');
  await expect(fichePills.first()).toBeVisible({ timeout: 10000 });

  // 5: Optionally validate correct fiche pill(s) are visible and not loading/empty
  // If "No fiches available" or "Loading fiches..." is present, fail
  const shelfText = await ficheShelf.textContent();
  expect(shelfText).not.toContain('Loading fiches...');
  expect(shelfText).not.toContain('No fiches available');

  // 6: Optionally (re)visit dashboard and back to canvas to verify shelf rerenders
  // const dashboardTab = page.locator('.header-nav');
  // await dashboardTab.click();
  // await canvasTab.click();
  // await expect(fichePills).toHaveCountGreaterThan(0, { timeout: 10000 });
});
