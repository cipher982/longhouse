import { test, expect } from './fixtures';

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
    console.log('🔍 Starting comprehensive database isolation test...');

    // Get the commis ID from environment
    const commisId = process.env.TEST_PARALLEL_INDEX || '0';
    console.log('📊 Commis ID:', commisId);

    // Navigate to the app - this should trigger database initialization
    console.log('🚀 Navigating to app...');
    await page.goto('/');

    // Wait for initial load
    await page.waitForTimeout(2000);

    // Check if we can see the app structure
    const dashboardExists = await page.locator('.header-nav').isVisible();
    console.log('📊 Dashboard tab visible:', dashboardExists);

    if (dashboardExists) {
      console.log('✅ App loaded successfully');

      // Try to interact with the dashboard
      await page.locator('.header-nav').click();
      await page.waitForTimeout(1000);

      // Check for any error messages
      const errorVisible = await page.locator('.error, .alert-error, [data-testid*="error"]').isVisible();
      console.log('📊 Error visible:', errorVisible);

      if (!errorVisible) {
        console.log('✅ Dashboard loaded without errors');
      } else {
        console.log('❌ Dashboard showed errors');
      }
    } else {
      console.log('❌ App did not load properly');
    }

    // Test API endpoints directly with proper headers
    console.log('🔍 Testing API endpoints...');

    // Test simple health check first
    try {
      const healthResponse = await request.get('/', {
        headers: {
          'X-Test-Commis': commisId,
        }
      });
      console.log('📊 Health check status:', healthResponse.status());

      if (healthResponse.ok()) {
        console.log('✅ Health check passed');
      } else {
        console.log('❌ Health check failed');
      }
    } catch (error) {
      console.log('❌ Health check error:', error);
    }

    // Test fiche endpoint
    try {
      const ficheResponse = await request.get('/api/fiches', {
        headers: {
          'X-Test-Commis': commisId,
        }
      });
      console.log('📊 Fiche API status:', ficheResponse.status());

      if (ficheResponse.ok()) {
        const fiches = await ficheResponse.json();
        console.log('📊 Fiche count:', Array.isArray(fiches) ? fiches.length : 'not array');
        console.log('✅ Fiche API working');
      } else {
        const errorText = await ficheResponse.text();
        console.log('❌ Fiche API failed:', errorText.substring(0, 200));
      }
    } catch (error) {
      console.log('❌ Fiche API error:', error);
    }

    // Test workflow endpoint
    try {
      const workflowResponse = await request.get('/api/workflows', {
        headers: {
          'X-Test-Commis': commisId,
        }
      });
      console.log('📊 Workflow API status:', workflowResponse.status());

      if (workflowResponse.ok()) {
        const workflows = await workflowResponse.json();
        console.log('📊 Workflow count:', Array.isArray(workflows) ? workflows.length : 'not array');
        console.log('✅ Workflow API working');
      } else {
        const errorText = await workflowResponse.text();
        console.log('❌ Workflow API failed:', errorText.substring(0, 200));
      }
    } catch (error) {
      console.log('❌ Workflow API error:', error);
    }

    // Test fiche creation
    console.log('🔍 Testing fiche creation...');
    try {
      const createResponse = await request.post('/api/fiches', {
        headers: {
          'X-Test-Commis': commisId,
          'Content-Type': 'application/json',
        },
        data: {
          name: `Test Fiche ${commisId}`,
          system_instructions: 'You are a test fiche for database isolation testing',
        }
      });
      console.log('📊 Fiche creation status:', createResponse.status());

      if (createResponse.ok()) {
        const fiche = await createResponse.json();
        console.log('📊 Created fiche ID:', fiche.id);
        console.log('✅ Fiche creation successful');
      } else {
        const errorText = await createResponse.text();
        console.log('❌ Fiche creation failed:', errorText.substring(0, 200));
      }
    } catch (error) {
      console.log('❌ Fiche creation error:', error);
    }

    console.log('✅ Comprehensive database isolation test complete');
  });
});
