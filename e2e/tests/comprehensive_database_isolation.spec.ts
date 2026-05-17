import { test, expect } from './fixtures';
import { waitForPageReady } from './helpers/ready-signals';

/**
 * COMPREHENSIVE DATABASE ISOLATION TEST
 *
 * This test validates that:
 * 1. Commis databases are properly isolated
 * 2. All database tables are created correctly
 * 3. API endpoints work with commis-specific databases
 * 4. Headers are properly transmitted and processed
 */

test.describe('Comprehensive Database Isolation', () => {
  test('Complete database isolation validation', async ({ page, request }) => {
    const commisId = process.env.TEST_PARALLEL_INDEX || '0';
    const otherCommisId = commisId === '0' ? '1' : '0';

    // Navigate to the app - this should trigger database initialization
    await page.goto('/automations');
    await waitForPageReady(page);
    await expect(page.locator('.header-nav')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="create-automation-btn"]')).toBeVisible({ timeout: 10000 });

    // Test API endpoints directly with proper headers
    // Test simple health check first
    const healthResponse = await request.get('/api/health', {
      headers: {
        'X-Test-Commis': commisId,
      }
    });
    expect(healthResponse.ok()).toBe(true);

    // Test the automation endpoint.
    const automationResponse = await request.get('/api/automations', {
      headers: {
        'X-Test-Commis': commisId,
      }
    });
    expect(automationResponse.ok()).toBe(true);
    const automations = await automationResponse.json();
    expect(Array.isArray(automations)).toBe(true);

    // Test automation creation.
    const createResponse = await request.post('/api/automations', {
      headers: {
        'X-Test-Commis': commisId,
        'Content-Type': 'application/json',
      },
      data: {
        system_instructions: 'You are a test automation for database isolation testing',
        task_instructions: 'Respond briefly',
        model: 'deepseek/deepseek-v4-pro',
      }
    });
    expect(createResponse.status()).toBe(201);
    const createdAutomation = await createResponse.json();
    expect(createdAutomation.id).toBeDefined();

    const automationListAfter = await request.get('/api/automations', {
      headers: {
        'X-Test-Commis': commisId,
      }
    });
    expect(automationListAfter.ok()).toBe(true);
    const automationsAfter = await automationListAfter.json();
    const idsAfter = Array.isArray(automationsAfter) ? automationsAfter.map((f: any) => f.id) : [];
    expect(idsAfter).toContain(createdAutomation.id);

    // Verify isolation by querying a different commis DB
    const otherListResponse = await request.get('/api/automations', {
      headers: {
        'X-Test-Commis': otherCommisId,
      }
    });
    expect(otherListResponse.ok()).toBe(true);
    const otherAutomations = await otherListResponse.json();
    const otherIds = Array.isArray(otherAutomations) ? otherAutomations.map((f: any) => f.id) : [];
    expect(otherIds).not.toContain(createdAutomation.id);
  });
});
