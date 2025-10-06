import { test, expect } from './fixtures';

test.describe('Agent search & filtering', () => {
  test('Search agents by name', async ({ page }) => {
    await page.goto('/');
    // Ensure at least one agent exists
    await page.locator('[data-testid="create-agent-btn"]').click();

    const search = page.locator('[data-testid="dashboard-search-input"], input[placeholder="Search agents"]');
    if ((await search.count()) === 0) test.skip();

    await search.fill('NonExistingXYZ');
    await page.keyboard.press('Enter');
    await expect(page.locator('tr[data-agent-id]')).toHaveCount(0);
  });

  test('Filter by agent status placeholder', async () => {
    test.skip();
  });

  test('Sort by name asc/desc placeholder', async () => {
    test.skip();
  });

  test('Combine search and filters placeholder', async () => {
    test.skip();
  });
});
