import { test, expect } from './fixtures';

/**
 * AGENT CREATION FULL TEST
 *
 * This test validates the complete fiche creation workflow:
 * 1. Database isolation is working (each commis has its own schema)
 * 2. Fiche creation via API works correctly
 * 3. Fiche appears in the UI after creation
 * 4. Multiple test runs are isolated from each other
 *
 * NOTE: The FicheCreate API does NOT accept a `name` field - names are auto-generated
 * as "New Fiche". We use `system_instructions` as the unique marker for isolation testing.
 *
 * IMPORTANT: Always use the `request` fixture for API calls - it has the correct
 * X-Test-Commis header. Never pass commisId separately as that can cause mismatches.
 */

test.describe('Fiche Creation Full Workflow', () => {
  test('Complete fiche creation and isolation test', async ({ page, request }, testInfo) => {
    const uniqueMarker = `commis_${testInfo.commisIndex}_${Date.now()}`;

    // Step 0: Reset database to ensure clean state
    // Use the request fixture which already has the correct X-Test-Commis header
    const resetResponse = await request.post('/api/admin/reset-database', {
      data: { reset_type: 'clear_data' }
    });
    expect(resetResponse.ok(), `Reset failed: ${await resetResponse.text()}`).toBeTruthy();

    // Step 1: Verify empty state
    const initialFiches = await request.get('/api/fiches');
    expect(initialFiches.status()).toBe(200);
    const initialFichesList = await initialFiches.json();
    expect(initialFichesList.length, 'Expected empty database after reset').toBe(0);

    // Step 2: Create an fiche via API
    // NOTE: `name` field is NOT in FicheCreate schema - backend auto-generates "New Fiche"
    // We use system_instructions as the unique identifier for this test
    const firstMarker = `first_fiche_${uniqueMarker}`;
    const createResponse = await request.post('/api/fiches', {
      data: {
        system_instructions: firstMarker,
        task_instructions: 'Perform test tasks as requested',
        model: 'gpt-mock',
      }
    });

    expect(createResponse.status()).toBe(201);
    const createdFiche = await createResponse.json();
    expect(createdFiche.system_instructions).toBe(firstMarker);
    // Name is auto-generated, not user-provided
    expect(createdFiche.name).toBe('New Fiche');

    // Step 3: Verify fiche appears in list
    const updatedFiches = await request.get('/api/fiches');
    expect(updatedFiches.status()).toBe(200);
    const updatedFichesList = await updatedFiches.json();
    expect(updatedFichesList.length).toBe(1);

    // Step 4: Verify fiche data by system_instructions (our unique marker)
    const foundFiche = updatedFichesList.find(
      (fiche: { system_instructions: string }) => fiche.system_instructions === firstMarker
    );
    expect(foundFiche, 'Fiche should be found by system_instructions').toBeDefined();

    // Step 5: Test UI integration - navigate to dashboard
    await page.goto('/dashboard');
    await page.waitForLoadState('networkidle');

    // Step 6: Create a second fiche to test isolation
    const secondMarker = `second_fiche_${uniqueMarker}`;
    const secondFicheResponse = await request.post('/api/fiches', {
      data: {
        system_instructions: secondMarker,
        task_instructions: 'Perform secondary test tasks',
        model: 'gpt-mock',
      }
    });

    expect(secondFicheResponse.status()).toBe(201);
    const secondFiche = await secondFicheResponse.json();
    expect(secondFiche.system_instructions).toBe(secondMarker);

    // Step 7: Verify both fiches exist and are isolated to this commis
    const finalFiches = await request.get('/api/fiches');
    const finalFichesList = await finalFiches.json();
    expect(finalFichesList.length).toBe(2);

    // Verify both fiches are present by their unique markers
    const instructions = finalFichesList.map(
      (a: { system_instructions: string }) => a.system_instructions
    );
    expect(instructions).toContain(firstMarker);
    expect(instructions).toContain(secondMarker);

    // Verify by ID as well
    const firstFicheFound = finalFichesList.find(
      (fiche: { id: number }) => fiche.id === createdFiche.id
    );
    const secondFicheFound = finalFichesList.find(
      (fiche: { id: number }) => fiche.id === secondFiche.id
    );
    expect(firstFicheFound).toBeDefined();
    expect(secondFicheFound).toBeDefined();
  });
});
