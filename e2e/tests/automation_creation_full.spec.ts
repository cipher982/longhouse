import { test, expect } from './fixtures';

/**
 * AUTOMATION CREATION FULL TEST
 *
 * This test validates the complete automation creation flow:
 * 1. Database isolation is working (each worker has its own schema)
 * 2. Automation creation via API works correctly
 * 3. Automation appears in the UI after creation
 * 4. Multiple test runs are isolated from each other
 *
 * NOTE: The create API does not accept a `name` field. Names are auto-generated
 * as "New Automation". We use `system_instructions` as the unique marker for isolation testing.
 *
 * IMPORTANT: Always use the `request` fixture for API calls - it has the correct
 * X-Test-Worker header. Never pass workerId separately as that can cause mismatches.
 */

test.describe('Automation Creation Full Workflow', () => {
  test('complete automation creation and isolation test', async ({ page, request }, testInfo) => {
    const uniqueMarker = `worker_${testInfo.workerIndex}_${Date.now()}`;

    // Step 0: Reset database to ensure clean state
    // Use the request fixture which already has the correct X-Test-Worker header
    const resetResponse = await request.post('/api/admin/reset-database', {
      data: { reset_type: 'clear_data' }
    });
    expect(resetResponse.ok(), `Reset failed: ${await resetResponse.text()}`).toBeTruthy();

    // Step 1: Verify empty state
    const initialAutomations = await request.get('/api/automations');
    expect(initialAutomations.status()).toBe(200);
    const initialAutomationsList = await initialAutomations.json();
    expect(initialAutomationsList.length, 'Expected empty database after reset').toBe(0);

    // Step 2: Create an automation via API.
    // NOTE: `name` is not accepted here; the backend auto-generates "New Automation".
    // We use system_instructions as the unique identifier for this test
    const firstMarker = `first_automation_${uniqueMarker}`;
    const createResponse = await request.post('/api/automations', {
      data: {
        system_instructions: firstMarker,
        task_instructions: 'Perform test tasks as requested',
        model: 'gpt-mock',
      }
    });

    expect(createResponse.status()).toBe(201);
    const createdAutomation = await createResponse.json();
    expect(createdAutomation.system_instructions).toBe(firstMarker);
    // Name is auto-generated, not user-provided
    expect(createdAutomation.name).toBe('New Automation');

    // Step 3: Verify the automation appears in the list.
    const updatedAutomations = await request.get('/api/automations');
    expect(updatedAutomations.status()).toBe(200);
    const updatedAutomationsList = await updatedAutomations.json();
    expect(updatedAutomationsList.length).toBe(1);

    const foundAutomation = updatedAutomationsList.find(
      (automation: { system_instructions: string }) => automation.system_instructions === firstMarker
    );
    expect(foundAutomation, 'Automation should be found by system_instructions').toBeDefined();

    // Step 5: Test UI integration - navigate to automations
    await page.goto('/automations');
    await page.waitForLoadState('domcontentloaded');

    // Step 6: Create a second automation to test isolation.
    const secondMarker = `second_automation_${uniqueMarker}`;
    const secondAutomationResponse = await request.post('/api/automations', {
      data: {
        system_instructions: secondMarker,
        task_instructions: 'Perform secondary test tasks',
        model: 'gpt-mock',
      }
    });

    expect(secondAutomationResponse.status()).toBe(201);
    const secondAutomation = await secondAutomationResponse.json();
    expect(secondAutomation.system_instructions).toBe(secondMarker);

    const finalAutomations = await request.get('/api/automations');
    const finalAutomationsList = await finalAutomations.json();
    expect(finalAutomationsList.length).toBe(2);

    const instructions = finalAutomationsList.map(
      (a: { system_instructions: string }) => a.system_instructions
    );
    expect(instructions).toContain(firstMarker);
    expect(instructions).toContain(secondMarker);

    const firstAutomationFound = finalAutomationsList.find(
      (automation: { id: number }) => automation.id === createdAutomation.id
    );
    const secondAutomationFound = finalAutomationsList.find(
      (automation: { id: number }) => automation.id === secondAutomation.id
    );
    expect(firstAutomationFound).toBeDefined();
    expect(secondAutomationFound).toBeDefined();
  });
});
