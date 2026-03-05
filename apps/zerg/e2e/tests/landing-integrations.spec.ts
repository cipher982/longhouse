import { test, expect } from '@playwright/test';

type ProviderExpectation = {
  name: string;
  description: string;
  status: 'Syncing now' | 'Coming soon';
};

const EXPECTED_PROVIDERS: ProviderExpectation[] = [
  {
    name: 'Claude Code',
    description: 'Full session sync with tool calls',
    status: 'Syncing now',
  },
  {
    name: 'Codex CLI',
    description: 'Full session sync with tool calls',
    status: 'Syncing now',
  },
  {
    name: 'Gemini CLI',
    description: 'Full session sync with tool calls',
    status: 'Syncing now',
  },
  {
    name: 'OpenCode',
    description: 'Open-source AI terminal agent',
    status: 'Coming soon',
  },
  {
    name: 'Cursor',
    description: 'IDE-integrated AI sessions',
    status: 'Coming soon',
  },
];

test.describe('Landing integrations claims', () => {
  test('provider cards match claimed statuses and descriptions', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#integrations')).toBeVisible({ timeout: 10_000 });

    for (const provider of EXPECTED_PROVIDERS) {
      const card = page.locator('.landing-provider-card', { hasText: provider.name }).first();
      await expect(card).toBeVisible();
      await expect(card.locator('.landing-provider-name')).toHaveText(provider.name);
      await expect(card.locator('.landing-provider-desc')).toHaveText(provider.description);
      await expect(card.locator('.landing-provider-status')).toHaveText(provider.status);
    }
  });
});
