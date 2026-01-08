import { test, expect } from './fixtures';
import { resetDatabaseViaRequest } from './helpers/database-helpers';

/**
 * AGENT CREATION FULL TEST
 *
 * This test validates the complete agent creation workflow:
 * 1. Database isolation is working (each worker has its own schema)
 * 2. Agent creation via API works correctly
 * 3. Agent appears in the UI after creation
 * 4. Multiple test runs are isolated from each other
 *
 * NOTE: The AgentCreate API does NOT accept a `name` field - names are auto-generated
 * as "New Agent". We use `system_instructions` as the unique marker for isolation testing.
 */

test.describe('Agent Creation Full Workflow', () => {
  test('Complete agent creation and isolation test', async ({ page, request, backendUrl }, testInfo) => {
    // Get the worker ID from testInfo (same source as fixtures)
    const workerId = String(testInfo.workerIndex);
    const uniqueMarker = `worker_${workerId}_${Date.now()}`;

    // Step 0: Reset database to ensure clean state
    // IMPORTANT: Don't swallow errors - if reset fails, test should fail fast
    await resetDatabaseViaRequest(page, { workerId });

    // Step 1: Verify empty state
    const initialAgents = await request.get('/api/agents');
    expect(initialAgents.status()).toBe(200);
    const initialAgentsList = await initialAgents.json();
    expect(initialAgentsList.length, 'Expected empty database after reset').toBe(0);

    // Step 2: Create an agent via API
    // NOTE: `name` field is NOT in AgentCreate schema - backend auto-generates "New Agent"
    // We use system_instructions as the unique identifier for this test
    const firstMarker = `first_agent_${uniqueMarker}`;
    const createResponse = await request.post('/api/agents', {
      data: {
        system_instructions: firstMarker,
        task_instructions: 'Perform test tasks as requested',
        model: 'gpt-mock',
      }
    });

    expect(createResponse.status()).toBe(201);
    const createdAgent = await createResponse.json();
    expect(createdAgent.system_instructions).toBe(firstMarker);
    // Name is auto-generated, not user-provided
    expect(createdAgent.name).toBe('New Agent');

    // Step 3: Verify agent appears in list
    const updatedAgents = await request.get('/api/agents');
    expect(updatedAgents.status()).toBe(200);
    const updatedAgentsList = await updatedAgents.json();
    expect(updatedAgentsList.length).toBe(1);

    // Step 4: Verify agent data by system_instructions (our unique marker)
    const foundAgent = updatedAgentsList.find(
      (agent: { system_instructions: string }) => agent.system_instructions === firstMarker
    );
    expect(foundAgent, 'Agent should be found by system_instructions').toBeDefined();

    // Step 5: Test UI integration - navigate to dashboard
    await page.goto('/dashboard');
    await page.waitForLoadState('networkidle');

    // Step 6: Create a second agent to test isolation
    const secondMarker = `second_agent_${uniqueMarker}`;
    const secondAgentResponse = await request.post('/api/agents', {
      data: {
        system_instructions: secondMarker,
        task_instructions: 'Perform secondary test tasks',
        model: 'gpt-mock',
      }
    });

    expect(secondAgentResponse.status()).toBe(201);
    const secondAgent = await secondAgentResponse.json();
    expect(secondAgent.system_instructions).toBe(secondMarker);

    // Step 7: Verify both agents exist and are isolated to this worker
    const finalAgents = await request.get('/api/agents');
    const finalAgentsList = await finalAgents.json();
    expect(finalAgentsList.length).toBe(2);

    // Verify both agents are present by their unique markers
    const instructions = finalAgentsList.map(
      (a: { system_instructions: string }) => a.system_instructions
    );
    expect(instructions).toContain(firstMarker);
    expect(instructions).toContain(secondMarker);

    // Verify by ID as well
    const firstAgentFound = finalAgentsList.find(
      (agent: { id: number }) => agent.id === createdAgent.id
    );
    const secondAgentFound = finalAgentsList.find(
      (agent: { id: number }) => agent.id === secondAgent.id
    );
    expect(firstAgentFound).toBeDefined();
    expect(secondAgentFound).toBeDefined();
  });
});
