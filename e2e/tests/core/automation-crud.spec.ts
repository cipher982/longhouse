/**
 * Automation CRUD Tests - Core Suite
 *
 * Tests basic automation create/read operations.
 *
 * CORE SUITE: 0 skipped, 0 flaky, retries: 0
 */

import { test, expect, type Page } from '../fixtures';
import { resetDatabase } from '../test-utils';

// Reset DB before each test for clean state
// Uses strict reset that throws on failure to fail fast
test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

test.describe('Automation CRUD - Core', () => {
  test('create automation - automation appears in automations', async ({ page }) => {
    await page.goto('/automations');

    const createBtn = page.locator('[data-testid="create-automation-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 10000 });
    await expect(createBtn).toBeEnabled({ timeout: 5000 });

    const automationRows = page.locator('tr[data-automation-id]');
    const initialCount = await automationRows.count();

    // Wait for API response
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/automations') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    // Use polling to wait for new row
    await expect.poll(async () => await automationRows.count(), { timeout: 10000 }).toBe(initialCount + 1);

    const newRow = automationRows.first();
    await expect(newRow).toBeVisible();

    const automationId = await newRow.getAttribute('data-automation-id');
    expect(automationId).toBeTruthy();
    expect(automationId).toMatch(/^\d+$/);
  });

  test('backend auto-generates placeholder name', async ({ request }) => {
    // Create an automation via API with no name field.
    const response = await request.post('/api/automations', {
      data: {
        system_instructions: 'Test instructions',
        task_instructions: 'Test task',
        model: 'gpt-5.2',
      },
    });

    expect(response.ok()).toBeTruthy();
    const automation = await response.json();

    expect(automation.name).toBe('New Automation');
  });

  test('idempotency key prevents duplicate creation', async ({ request }) => {
    const idempotencyKey = `test-${Date.now()}-${Math.random()}`;

    // Create an automation with an idempotency key.
    const response1 = await request.post('/api/automations', {
      headers: { 'Idempotency-Key': idempotencyKey },
      data: {
        system_instructions: 'Test instructions',
        task_instructions: 'Test task',
        model: 'gpt-5.2',
      },
    });
    expect(response1.ok()).toBeTruthy();
    const automation1 = await response1.json();

    // Retry with same idempotency key (simulates double-click)
    const response2 = await request.post('/api/automations', {
      headers: { 'Idempotency-Key': idempotencyKey },
      data: {
        system_instructions: 'Different instructions',
        task_instructions: 'Different task',
        model: 'gpt-5.2',
      },
    });
    expect(response2.ok()).toBeTruthy();
    const automation2 = await response2.json();

    expect(automation2.id).toBe(automation1.id);
    expect(automation2.name).toBe(automation1.name);
  });
});
