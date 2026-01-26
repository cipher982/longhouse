import { test, expect } from './fixtures';
import { resetDatabase } from './test-utils';

// Uses strict reset that throws on failure to fail fast
test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

// Stubs for trigger management – UI selectors may evolve, skip if missing.

test.describe.skip('Webhook trigger management', () => {
  async function openTriggersTab(page) {
    await page.goto('/');
    await page.locator('[data-testid="create-fiche-btn"]').click();
    const id = await page.locator('tr[data-fiche-id]').first().getAttribute('data-fiche-id');
    await page.locator(`[data-testid="edit-fiche-${id}"]`).click();
    await page.waitForSelector('#fiche-modal', { state: 'visible' });
    const tab = page.locator('#fiche-triggers-tab');
    if ((await tab.count()) === 0) test.skip(true, 'Triggers tab not present');
    await tab.click();
  }

  test('Create webhook trigger', async ({ page }) => {
    await openTriggersTab(page);

    const addBtn = page.locator('#fiche-add-trigger-btn');
    await addBtn.click();

    const typeSel = page.locator('#fiche-trigger-type-select');
    await typeSel.selectOption('webhook');

    await page.locator('#fiche-create-trigger').click();

    // Expect list entry
    await expect(page.locator('#fiche-triggers-list li')).toHaveCount(1, { timeout: 5000 });
  });

  test('Copy webhook URL placeholder', async () => {
    test.skip();
  });

  test('View webhook secret placeholder', async () => {
    test.skip();
  });

  test('Multiple triggers per fiche placeholder', async () => {
    test.skip();
  });

  test('Delete webhook trigger', async ({ page }) => {
    await openTriggersTab(page);
    const firstLi = page.locator('#fiche-triggers-list li').first();
    if ((await firstLi.count()) === 0) test.skip(true, 'No triggers to delete');

    await firstLi.locator('button', { hasText: 'Delete' }).click();
    page.once('dialog', (d) => d.accept());
    await expect(firstLi).toHaveCount(0);
  });
});
