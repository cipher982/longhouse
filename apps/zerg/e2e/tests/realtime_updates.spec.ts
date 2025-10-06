import { test, expect } from './fixtures';

// Ensure every test in this file starts with an empty DB so row counts are
// deterministic across parallel pages.
test.beforeEach(async ({ request }) => {
  await request.post('http://localhost:8001/admin/reset-database');
});

test('Dashboard live update placeholder', async ({ browser }) => {
  // Open two tabs to simulate multi-tab sync
  const context = await browser.newContext();
  const page1 = await context.newPage();
  await page1.goto('/');
  const page2 = await context.newPage();
  await page2.goto('/');

  // Trigger create in page1
  await page1.locator('[data-testid="create-agent-btn"]').click();

  // Expect row appears in page2 after some time
  await expect(page2.locator('tr[data-agent-id]')).toHaveCount(1, { timeout: 15_000 });
});

test('WebSocket connection placeholder', async () => {
  test.skip();
});

test('Message streaming placeholder', async () => {
  test.skip();
});

test('Connection recovery placeholder', async () => {
  test.skip();
});

test('Presence indicators placeholder', async () => {
  test.skip();
});
