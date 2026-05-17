import { test, expect } from './fixtures';
import { resetDatabase } from './test-utils';
import { waitForAutomationsReady } from './helpers/test-helpers';

test.describe('Automation Creation', () => {
  // Uses strict reset that throws on failure to fail fast
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('creates automations with "New Automation" placeholder name', async ({ page }) => {
    await waitForAutomationsReady(page);

    // Wait for create button to be ready
    await expect(page.locator('[data-testid="create-automation-btn"]')).toBeVisible({ timeout: 5000 });

    // Create first automation with deterministic wait for API response
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/automations') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      page.click('[data-testid="create-automation-btn"]'),
    ]);

    // Create second automation with deterministic wait
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/automations') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      page.click('[data-testid="create-automation-btn"]'),
    ]);

    // Create third automation with deterministic wait
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/automations') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      page.click('[data-testid="create-automation-btn"]'),
    ]);

    // Wait for all 3 automation rows to appear.
    await expect(page.locator('#automations-table-body tr[data-automation-id]')).toHaveCount(3, { timeout: 10000 });

    const automationRows = page.locator('#automations-table-body tr[data-automation-id]');

    const firstAutomationName = await automationRows.nth(0).locator('td[data-label="Name"]').textContent();
    const secondAutomationName = await automationRows.nth(1).locator('td[data-label="Name"]').textContent();
    const thirdAutomationName = await automationRows.nth(2).locator('td[data-label="Name"]').textContent();

    expect(firstAutomationName).toBe('New Automation');
    expect(secondAutomationName).toBe('New Automation');
    expect(thirdAutomationName).toBe('New Automation');
  });

  test('backend auto-generates "New Automation" placeholder name', async ({ request }) => {
    // Create an automation without a name field.
    const response = await request.post('/api/automations', {
      data: {
        system_instructions: 'Test instructions',
        task_instructions: 'Test task',
        model: 'deepseek/deepseek-v4-pro'
      }
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
        model: 'deepseek/deepseek-v4-pro'
      }
    });
    expect(response1.ok()).toBeTruthy();
    const automation1 = await response1.json();

    // Retry with same idempotency key (simulates double-click)
    const response2 = await request.post('/api/automations', {
      headers: { 'Idempotency-Key': idempotencyKey },
      data: {
        system_instructions: 'Different instructions',
        task_instructions: 'Different task',
        model: 'deepseek/deepseek-v4-pro'
      }
    });
    expect(response2.ok()).toBeTruthy();
    const automation2 = await response2.json();

    expect(automation2.id).toBe(automation1.id);
    expect(automation2.name).toBe(automation1.name);
  });
});
