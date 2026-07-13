import { test, expect } from '@playwright/test';

type ProviderExpectation = {
  name: string;
  // sync, launch & send, interrupt, steer mid-turn, resume
  cells: Array<'yes' | 'no'>;
};

const EXPECTED_PROVIDERS: ProviderExpectation[] = [
  { name: 'Claude Code', cells: ['yes', 'yes', 'yes', 'yes', 'yes'] },
  { name: 'Codex CLI', cells: ['yes', 'yes', 'yes', 'yes', 'yes'] },
  { name: 'Cursor Agent', cells: ['yes', 'yes', 'yes', 'no', 'yes'] },
  { name: 'OpenCode', cells: ['yes', 'yes', 'yes', 'no', 'no'] },
  { name: 'Antigravity CLI', cells: ['yes', 'yes', 'no', 'no', 'no'] },
];

test.describe('Landing integrations claims', () => {
  test('provider capability matrix matches the claimed contract', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#providers')).toBeVisible({ timeout: 10_000 });

    const table = page.locator('.landing-providers-table');
    await expect(table).toBeVisible();

    const rows = table.locator('tbody tr');
    await expect(rows).toHaveCount(EXPECTED_PROVIDERS.length);

    for (const [index, provider] of EXPECTED_PROVIDERS.entries()) {
      const row = rows.nth(index);
      await expect(row.locator('th')).toHaveText(provider.name);
      const cells = row.locator('td.landing-providers-cell');
      await expect(cells).toHaveCount(provider.cells.length);
      for (const [cellIndex, expected] of provider.cells.entries()) {
        await expect(cells.nth(cellIndex)).toHaveClass(new RegExp(`\\b${expected}\\b`));
      }
    }
  });
});
