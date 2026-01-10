/**
 * Agent CRUD Tests - Core Suite
 *
 * Tests basic agent create/read operations.
 *
 * CORE SUITE: 0 skipped, 0 flaky, retries: 0
 */

import { test, expect, type Page } from '../fixtures';

// Reset DB before each test for clean state
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
});

test.describe('Agent CRUD - Core', () => {
  test('create agent - agent appears in dashboard', async ({ page }) => {
    await page.goto('/');

    const createBtn = page.locator('[data-testid="create-agent-btn"]');
    await expect(createBtn).toBeVisible({ timeout: 10000 });
    await expect(createBtn).toBeEnabled({ timeout: 5000 });

    const agentRows = page.locator('tr[data-agent-id]');
    const initialCount = await agentRows.count();

    // Wait for API response
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/agents') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      createBtn.click(),
    ]);

    // Use polling to wait for new row
    await expect.poll(async () => await agentRows.count(), { timeout: 10000 }).toBe(initialCount + 1);

    const newRow = agentRows.first();
    await expect(newRow).toBeVisible();

    const agentId = await newRow.getAttribute('data-agent-id');
    expect(agentId).toBeTruthy();
    expect(agentId).toMatch(/^\d+$/);
  });

  test('backend auto-generates placeholder name', async ({ request }) => {
    // Create agent via API (no name field sent)
    const response = await request.post('/api/agents', {
      data: {
        system_instructions: 'Test instructions',
        task_instructions: 'Test task',
        model: 'gpt-5.2',
      },
    });

    expect(response.ok()).toBeTruthy();
    const agent = await response.json();

    // Should have auto-generated name "New Agent"
    expect(agent.name).toBe('New Agent');
  });

  test('idempotency key prevents duplicate creation', async ({ request }) => {
    const idempotencyKey = `test-${Date.now()}-${Math.random()}`;

    // Create agent with idempotency key
    const response1 = await request.post('/api/agents', {
      headers: { 'Idempotency-Key': idempotencyKey },
      data: {
        system_instructions: 'Test instructions',
        task_instructions: 'Test task',
        model: 'gpt-5.2',
      },
    });
    expect(response1.ok()).toBeTruthy();
    const agent1 = await response1.json();

    // Retry with same idempotency key (simulates double-click)
    const response2 = await request.post('/api/agents', {
      headers: { 'Idempotency-Key': idempotencyKey },
      data: {
        system_instructions: 'Different instructions',
        task_instructions: 'Different task',
        model: 'gpt-5.2',
      },
    });
    expect(response2.ok()).toBeTruthy();
    const agent2 = await response2.json();

    // Should return the SAME agent (not create a new one)
    expect(agent2.id).toBe(agent1.id);
    expect(agent2.name).toBe(agent1.name);
  });
});
