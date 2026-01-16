import { test, expect } from './fixtures';
import { resetDatabase } from './test-utils';

test.describe('Agent Creation', () => {
  // Uses strict reset that throws on failure to fail fast
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('creates agents with "New Agent" placeholder name', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Wait for create button to be ready
    await expect(page.locator('[data-testid="create-agent-btn"]')).toBeVisible({ timeout: 5000 });

    // Create first agent with deterministic wait for API response
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/agents') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      page.click('[data-testid="create-agent-btn"]'),
    ]);

    // Create second agent with deterministic wait
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/agents') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      page.click('[data-testid="create-agent-btn"]'),
    ]);

    // Create third agent with deterministic wait
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/agents') && r.request().method() === 'POST' && r.status() === 201,
        { timeout: 10000 }
      ),
      page.click('[data-testid="create-agent-btn"]'),
    ]);

    // Wait for all 3 agent rows to appear
    await expect(page.locator('#agents-table-body tr[data-agent-id]')).toHaveCount(3, { timeout: 10000 });

    // Get all agent rows
    const agentRows = page.locator('#agents-table-body tr[data-agent-id]');

    // Check agent names are all "New Agent"
    const firstAgentName = await agentRows.nth(0).locator('td[data-label="Name"]').textContent();
    const secondAgentName = await agentRows.nth(1).locator('td[data-label="Name"]').textContent();
    const thirdAgentName = await agentRows.nth(2).locator('td[data-label="Name"]').textContent();

    // Should all be "New Agent"
    expect(firstAgentName).toBe('New Agent');
    expect(secondAgentName).toBe('New Agent');
    expect(thirdAgentName).toBe('New Agent');
  });

  test('backend auto-generates "New Agent" placeholder name', async ({ request }) => {
    // Create agent (no name field sent)
    const response = await request.post('/api/agents', {
      data: {
        system_instructions: 'Test instructions',
        task_instructions: 'Test task',
        model: 'gpt-5.2'
      }
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
        model: 'gpt-5.2'
      }
    });
    expect(response1.ok()).toBeTruthy();
    const agent1 = await response1.json();

    // Retry with same idempotency key (simulates double-click)
    const response2 = await request.post('/api/agents', {
      headers: { 'Idempotency-Key': idempotencyKey },
      data: {
        system_instructions: 'Different instructions',
        task_instructions: 'Different task',
        model: 'gpt-5.2'
      }
    });
    expect(response2.ok()).toBeTruthy();
    const agent2 = await response2.json();

    // Should return the SAME agent (not create a new one)
    expect(agent2.id).toBe(agent1.id);
    expect(agent2.name).toBe(agent1.name);
  });
});
