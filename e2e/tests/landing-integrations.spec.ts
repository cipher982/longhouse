import { test, expect } from '@playwright/test';

type ProviderExpectation = {
  name: string;
  description: string;
  status: string;
};

const EXPECTED_PROVIDERS: ProviderExpectation[] = [
  {
    name: 'Claude Code',
    description: 'Archive, search, and strongest control path',
    status: 'Strongest today',
  },
  {
    name: 'Codex CLI',
    description: 'Archive, search, and Longhouse launch path',
    status: 'Control-ready',
  },
  {
    name: 'Cursor Agent',
    description: 'Archive, launch, send, interrupt, and terminate',
    status: 'Helm + Console',
  },
  {
    name: 'Antigravity CLI',
    description: 'Archive, launch, and hook-backed phase signals',
    status: 'Observe-only today',
  },
];

test.describe('Landing integrations claims', () => {
  test('provider cards match claimed statuses and descriptions', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#providers')).toBeVisible({ timeout: 10_000 });

    for (const provider of EXPECTED_PROVIDERS) {
      const card = page.locator('.landing-provider-card', { hasText: provider.name }).first();
      await expect(card).toBeVisible();
      await expect(card.locator('.landing-provider-name')).toHaveText(provider.name);
      await expect(card.locator('.landing-provider-desc')).toHaveText(provider.description);
      await expect(card.locator('.landing-provider-status')).toHaveText(provider.status);
    }
  });
});
