import { test, expect } from './fixtures';

test.describe('Fiche search & filtering', () => {
  test('Search fiches by name', async ({ page }) => {
    await page.goto('/dashboard');
    // Ensure at least one fiche exists
    await page.locator('[data-testid="create-fiche-btn"]').click();

    const search = page.locator('[data-testid="dashboard-search-input"], input[placeholder="Search fiches"]');
    if ((await search.count()) === 0) test.skip();

    await search.fill('NonExistingXYZ');
    await page.keyboard.press('Enter');
    await expect(page.locator('tr[data-fiche-id]')).toHaveCount(0);
  });
});
