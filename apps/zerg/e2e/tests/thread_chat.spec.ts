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

async function createAgentAndGetId(page: Page): Promise<string> {
  await page.goto('/');

  const createBtn = page.locator('[data-testid="create-agent-btn"]');
  await expect(createBtn).toBeVisible({ timeout: 10000 });
  await expect(createBtn).toBeEnabled({ timeout: 5000 });

  // Capture API response to get the ACTUAL created agent ID
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/api/agents') && r.request().method() === 'POST' && r.status() === 201,
      { timeout: 10000 }
    ),
    createBtn.click(),
  ]);

  const body = await response.json();
  const agentId = String(body.id);

  if (!agentId || agentId === 'undefined') {
    throw new Error(`Failed to get agent ID from API response: ${JSON.stringify(body)}`);
  }

  const row = page.locator(`tr[data-agent-id="${agentId}"]`);
  await expect(row).toBeVisible({ timeout: 10000 });
  return agentId;
}

async function navigateToChat(page: Page, agentId: string): Promise<void> {
  await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
  await page.waitForURL((url) => url.pathname.includes(`/agent/${agentId}/thread`), { timeout: 10000 });
  await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });
  await expect(page.locator('[data-testid="chat-input"]')).toBeEnabled({ timeout: 5000 });
}

test.describe('Thread & Chat - Edge Cases', () => {
  test.skip('Wait for and verify agent response (placeholder)', async ({ page }) => {
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
