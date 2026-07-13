import { test, expect } from '@playwright/test';

type ProviderExpectation = {
  name: string;
  description: string;
  status: string;
};

const EXPECTED_PROVIDERS: ProviderExpectation[] = [
  {
    name: 'Claude Code',
    description: 'Launch, send, steer, interrupt, and resume',
    status: 'Full control',
  },
  {
    name: 'Codex CLI',
    description: 'Launch, send, steer, interrupt, and resume',
    status: 'Full control',
  },
  {
    name: 'Cursor Agent',
    description: 'Launch, send, interrupt, terminate, and resume',
    status: 'No mid-turn steering',
  },
  {
    name: 'OpenCode',
    description: 'Launch, send, interrupt, and terminate',
    status: 'No steering or resume',
  },
  {
    name: 'Antigravity CLI',
    description: 'Local launch and send',
    status: 'Limited control',
  },
];

test.describe('Landing integrations claims', () => {
  test('provider cards match claimed statuses and descriptions', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#providers')).toBeVisible({ timeout: 10_000 });

    for (const provider of EXPECTED_PROVIDERS) {
      const row = page.locator('.landing-provider-row', { hasText: provider.name }).first();
      await expect(row).toBeVisible();
      await expect(row.locator('.landing-provider-row-name')).toHaveText(provider.name);
      await expect(row.locator('.landing-provider-row-desc')).toHaveText(provider.description);
      await expect(row.locator('.landing-provider-row-status')).toHaveText(provider.status);
    }
  });
});
