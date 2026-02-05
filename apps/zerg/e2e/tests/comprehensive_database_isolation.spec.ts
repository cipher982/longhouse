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
    await page.goto('/dashboard');
    await waitForPageReady(page);
    await expect(page.locator('.header-nav')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="create-fiche-btn"]')).toBeVisible({ timeout: 10000 });

    // Test API endpoints directly with proper headers
    // Test simple health check first
    const healthResponse = await request.get('/api/health', {
      headers: {
        'X-Test-Commis': commisId,
      }
    });
    expect(healthResponse.ok()).toBe(true);

    // Test fiche endpoint
    const ficheResponse = await request.get('/api/fiches', {
      headers: {
        'X-Test-Commis': commisId,
      }
    });
    expect(ficheResponse.ok()).toBe(true);
    const fiches = await ficheResponse.json();
    expect(Array.isArray(fiches)).toBe(true);

    // Test fiche creation
    const createResponse = await request.post('/api/fiches', {
      headers: {
        'X-Test-Commis': commisId,
        'Content-Type': 'application/json',
      },
      data: {
        system_instructions: 'You are a test fiche for database isolation testing',
        task_instructions: 'Respond briefly',
        model: 'gpt-5.2',
      }
    });
    expect(createResponse.status()).toBe(201);
    const createdFiche = await createResponse.json();
    expect(createdFiche.id).toBeDefined();

    const ficheListAfter = await request.get('/api/fiches', {
      headers: {
        'X-Test-Commis': commisId,
      }
    });
    expect(ficheListAfter.ok()).toBe(true);
    const fichesAfter = await ficheListAfter.json();
    const idsAfter = Array.isArray(fichesAfter) ? fichesAfter.map((f: any) => f.id) : [];
    expect(idsAfter).toContain(createdFiche.id);

    // Verify isolation by querying a different commis DB
    const otherListResponse = await request.get('/api/fiches', {
      headers: {
        'X-Test-Commis': otherCommisId,
      }
    });
    expect(otherListResponse.ok()).toBe(true);
    const otherFiches = await otherListResponse.json();
    const otherIds = Array.isArray(otherFiches) ? otherFiches.map((f: any) => f.id) : [];
    expect(otherIds).not.toContain(createdFiche.id);
  });
});
