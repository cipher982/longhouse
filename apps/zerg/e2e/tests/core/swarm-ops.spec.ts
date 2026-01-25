/**
 * Swarm Ops Signal Tests - Core Suite
 *
 * Validates that Swarm Ops surfaces real signal for recent runs.
 */

import { test, expect } from '../fixtures';
import { resetDatabase } from '../test-utils';

test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

test.describe('Swarm Ops - Core', () => {
  test('shows signal and last event for seeded runs', async ({ page, request }) => {
    test.setTimeout(45000);

    const seedResponse = await request.post('/api/admin/seed-scenario', {
      data: { name: 'swarm-mvp', clean: true },
    });

    expect(seedResponse.ok()).toBeTruthy();

    await page.goto('/swarm');

    await page.getByRole('button', { name: 'All' }).click();

    const needsApproval = page.locator('.swarm-ops-item', { hasText: 'Needs approval' });
    await expect(needsApproval).toBeVisible({ timeout: 20000 });

    const hardStop = page.locator('.swarm-ops-item', { hasText: 'disk full on /mnt/storage' });
    await expect(hardStop).toBeVisible({ timeout: 20000 });

    await needsApproval.click();

    const detailPanel = page.locator('.swarm-ops-detail');
    await expect(detailPanel).toContainText('Signal', { timeout: 10000 });
    await expect(detailPanel).toContainText('Needs approval', { timeout: 10000 });
    await expect(detailPanel).toContainText('Last event', { timeout: 10000 });
  });
});
