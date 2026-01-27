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
import { resetDatabase } from './test-utils';

// Reset DB before each test
// Uses strict reset that throws on failure to fail fast
test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

async function createFicheAndGetId(page: Page): Promise<string> {
  await page.goto('/');

  const createBtn = page.locator('[data-testid="create-fiche-btn"]');
  await expect(createBtn).toBeVisible({ timeout: 10000 });
  await expect(createBtn).toBeEnabled({ timeout: 5000 });

  // Capture API response to get the ACTUAL created fiche ID
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/api/fiches') && r.request().method() === 'POST' && r.status() === 201,
      { timeout: 10000 }
    ),
    createBtn.click(),
  ]);

  const body = await response.json();
  const ficheId = String(body.id);

  if (!ficheId || ficheId === 'undefined') {
    throw new Error(`Failed to get fiche ID from API response: ${JSON.stringify(body)}`);
  }

  const row = page.locator(`tr[data-fiche-id="${ficheId}"]`);
  await expect(row).toBeVisible({ timeout: 10000 });
  return ficheId;
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
