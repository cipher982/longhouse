import { test, expect } from '@playwright/test';

type ProviderExpectation = {
  name: string;
  // search, launch, interrupt, steer mid-turn, resume
  capabilities: Array<'true' | 'false'>;
};

const EXPECTED_PROVIDERS: ProviderExpectation[] = [
  { name: 'Claude Code', capabilities: ['true', 'true', 'true', 'true', 'true'] },
  { name: 'Codex CLI', capabilities: ['true', 'true', 'true', 'true', 'true'] },
  { name: 'Cursor Agent', capabilities: ['true', 'true', 'true', 'false', 'true'] },
  { name: 'OpenCode', capabilities: ['true', 'true', 'true', 'false', 'true'] },
  { name: 'Pi Agent', capabilities: ['true', 'true', 'true', 'false', 'false'] },
  { name: 'Antigravity CLI', capabilities: ['true', 'false', 'false', 'false', 'false'] },
];

test.describe('Landing integrations claims', () => {
  test('provider capability rails match the claimed contract', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#providers')).toBeVisible({ timeout: 10_000 });

    const rows = page.locator('.landing-provider-rail');
    await expect(rows).toHaveCount(EXPECTED_PROVIDERS.length);

    for (const [index, provider] of EXPECTED_PROVIDERS.entries()) {
      const row = rows.nth(index);
      await expect(row.locator('.landing-provider-row-name')).toHaveText(provider.name);
      const capabilities = row.locator('.landing-provider-capability');
      await expect(capabilities).toHaveCount(provider.capabilities.length);
      for (const [capabilityIndex, expected] of provider.capabilities.entries()) {
        await expect(capabilities.nth(capabilityIndex)).toHaveAttribute('data-supported', expected);
      }
    }
  });
});
