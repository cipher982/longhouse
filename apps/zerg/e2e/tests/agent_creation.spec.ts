import { test, expect } from './fixtures';

test.describe('Agent Creation', () => {
  test.beforeEach(async ({ request }) => {
    await request.post('/admin/reset-database');
  });

  test('creates agents with sequential ID-based names', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Create first agent
    await page.click('#create-agent-button');
    await page.waitForTimeout(500);

    // Create second agent
    await page.click('#create-agent-button');
    await page.waitForTimeout(500);

    // Create third agent
    await page.click('#create-agent-button');
    await page.waitForTimeout(500);

    // Wait for agents to appear
    await page.waitForSelector('table#agents-table tbody tr[data-agent-id]');

    // Get all agent rows
    const agentRows = page.locator('table#agents-table tbody tr[data-agent-id]');
    const count = await agentRows.count();
    expect(count).toBeGreaterThanOrEqual(3);

    // Check agent names are sequential
    const firstAgentName = await agentRows.nth(0).locator('td[data-label="Name"]').textContent();
    const secondAgentName = await agentRows.nth(1).locator('td[data-label="Name"]').textContent();
    const thirdAgentName = await agentRows.nth(2).locator('td[data-label="Name"]').textContent();

    // Should be "Agent #1", "Agent #2", "Agent #3" (IDs are sequential in clean DB)
    expect(firstAgentName).toMatch(/^Agent #\d+$/);
    expect(secondAgentName).toMatch(/^Agent #\d+$/);
    expect(thirdAgentName).toMatch(/^Agent #\d+$/);

    // Verify they're NOT all the same name
    const names = new Set([firstAgentName, secondAgentName, thirdAgentName]);
    expect(names.size).toBe(3);  // All three names should be unique
  });

  test('backend prevents duplicate agent names for same owner', async ({ request }) => {
    // Create agent with specific name
    const response1 = await request.post('/api/agents', {
      data: {
        name: 'Test Agent',
        system_instructions: 'Test instructions',
        task_instructions: 'Test task',
        model: 'gpt-4o'
      }
    });
    expect(response1.ok()).toBeTruthy();

    // Try to create another agent with same name
    const response2 = await request.post('/api/agents', {
      data: {
        name: 'Test Agent',
        system_instructions: 'Different instructions',
        task_instructions: 'Different task',
        model: 'gpt-4o'
      }
    });

    // Should fail with 400 or 409 (Conflict)
    expect(response2.status()).toBeGreaterThanOrEqual(400);
    expect(response2.status()).toBeLessThan(500);
  });

  test('replaces generic "New Agent" with ID-based name', async ({ request }) => {
    // Create agent with generic name
    const response = await request.post('/api/agents', {
      data: {
        name: 'New Agent',
        system_instructions: 'Test instructions',
        task_instructions: 'Test task',
        model: 'gpt-4o'
      }
    });

    expect(response.ok()).toBeTruthy();
    const agent = await response.json();

    // Should have been renamed to "Agent #<id>"
    expect(agent.name).toMatch(/^Agent #\d+$/);
    expect(agent.name).toBe(`Agent #${agent.id}`);
  });
});
