/**
 * Chat Functional Tests
 *
 * NOTE: Core chat tests have been consolidated into happy-paths.spec.ts
 * This file contains only specialized tests not covered there.
 *
 * For core chat flows, see:
 * - happy-paths.spec.ts: SMOKE 3-4 (send message, input clears)
 * - happy-paths.spec.ts: PERSIST 1-2 (message persistence)
 * - happy-paths.spec.ts: CHAT 1-2 (follow-up, empty state)
 */

import { test, expect, type Page } from './fixtures';

// Reset DB before each test
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
});

async function createAgentAndGetId(page: Page): Promise<string> {
  await page.goto('/');

  const createBtn = page.locator('[data-testid="create-agent-btn"]');
  await expect(createBtn).toBeVisible({ timeout: 10000 });

  await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/api/agents') && r.request().method() === 'POST' && r.status() === 201,
      { timeout: 10000 }
    ),
    createBtn.click(),
  ]);

  const row = page.locator('tr[data-agent-id]').first();
  await expect(row).toBeVisible({ timeout: 5000 });
  return (await row.getAttribute('data-agent-id')) as string;
}

test.describe('Chat Functional Tests - Edge Cases', () => {
  // Core "send message" test is in happy-paths.spec.ts SMOKE 3
  // Core "persistence" test is in happy-paths.spec.ts PERSIST 1-2
  // Core "follow-up" test is in happy-paths.spec.ts CHAT 1

  test.skip('Send multiple messages and verify conversation state', async ({ page }) => {
    // Skipped: This test waits for LLM responses between messages, causing timeouts
    // Enable when mock server is available
    test.skip(true, 'LLM streaming not stubbed – skipping until mock server available');
  });

  test.skip('Thread switching preserves individual conversation state', async ({ page }) => {
    // Skipped: This test waits for LLM responses between messages
    // Thread switching without LLM is tested in happy-paths.spec.ts THREAD 2-3
    test.skip(true, 'LLM streaming not stubbed – skipping until mock server available');
  });
});
