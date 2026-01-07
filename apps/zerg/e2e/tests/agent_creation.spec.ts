import { test, expect } from './fixtures';

test.describe('Agent Creation', () => {
  test.beforeEach(async ({ request }) => {
    await request.post('/admin/reset-database');
  });

  test('creates agents with "New Agent" placeholder name', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Create first agent
    await page.click('[data-testid="create-agent-btn"]');
    await page.waitForTimeout(500);

    // Create second agent
    await page.click('[data-testid="create-agent-btn"]');
    await page.waitForTimeout(500);

    // Create third agent
    await page.click('[data-testid="create-agent-btn"]');
    await page.waitForTimeout(500);

    // Wait for agents to appear
    await page.waitForSelector('#agents-table-body tr[data-agent-id]');

    // Get all agent rows
    const agentRows = page.locator('#agents-table-body tr[data-agent-id]');
    const count = await agentRows.count();
    expect(count).toBeGreaterThanOrEqual(3);

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
