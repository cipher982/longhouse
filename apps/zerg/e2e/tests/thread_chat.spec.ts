/**
 * Thread & Chat Tests
 *
 * NOTE: Most thread/chat tests have been consolidated into happy-paths.spec.ts
 * This file contains only specialized edge case tests not covered there.
 *
 * For core thread/chat flows, see:
 * - happy-paths.spec.ts: SMOKE, THREAD, PERSIST, CHAT sections
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

async function navigateToChat(page: Page, ficheId: string): Promise<void> {
  await page.locator(`[data-testid="chat-fiche-${ficheId}"]`).click();
  await page.waitForURL((url) => url.pathname.includes(`/fiche/${ficheId}/thread`), { timeout: 10000 });
  await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });
  await expect(page.locator('[data-testid="chat-input"]')).toBeEnabled({ timeout: 5000 });
}

test.describe('Thread & Chat - Edge Cases', () => {
  test.skip('Wait for and verify fiche response (placeholder)', async ({ page }) => {
    // Skipped: LLM streaming not stubbed - requires mock server
    test.skip(true, 'LLM streaming not stubbed – skipping until mock server available');
  });

  test.skip('Delete thread and verify removal', async ({ page }) => {
    // Skipped: Thread deletion is not implemented in the UI yet
    test.skip(true, 'Thread deletion is not implemented in the UI yet');
  });

  // Thread switching preserves state is tested in happy-paths.spec.ts THREAD tests
  // Message persistence is tested in happy-paths.spec.ts PERSIST tests
  // Follow-up messages are tested in happy-paths.spec.ts CHAT tests
});
