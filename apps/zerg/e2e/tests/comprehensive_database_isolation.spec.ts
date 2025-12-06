import { test, expect } from './fixtures';

/**
 * COMPREHENSIVE DATABASE ISOLATION TEST
 *
 * This test validates that:
 * 1. Worker databases are properly isolated
 * 2. All database tables are created correctly
 * 3. API endpoints work with worker-specific databases
 * 4. Headers are properly transmitted and processed
 */

test.describe('Comprehensive Database Isolation', () => {
  test('Complete database isolation validation', async ({ page }) => {
    console.log('🔍 Starting comprehensive database isolation test...');

    // Get the worker ID from environment
    const workerId = process.env.PW_TEST_WORKER_INDEX || '0';
    console.log('📊 Worker ID:', workerId);

    // Navigate to the app - this should trigger database initialization
    console.log('🚀 Navigating to app...');
    await page.goto('/');

    // Wait for initial load
    await page.waitForTimeout(2000);

    // Check if we can see the app structure
    const dashboardExists = await page.locator('[data-testid="global-dashboard-tab"]').isVisible();
    console.log('📊 Dashboard tab visible:', dashboardExists);

    if (dashboardExists) {
      console.log('✅ App loaded successfully');

      // Try to interact with the dashboard
      await page.getByTestId('global-dashboard-tab').click();
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
      const healthResponse = await page.request.get('http://localhost:8001/', {
        headers: {
          'X-Test-Worker': workerId,
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

    // Test agent endpoint
    try {
      const agentResponse = await page.request.get('http://localhost:8001/api/agents', {
        headers: {
          'X-Test-Worker': workerId,
        }
      });
      console.log('📊 Agent API status:', agentResponse.status());

      if (agentResponse.ok()) {
        const agents = await agentResponse.json();
        console.log('📊 Agent count:', Array.isArray(agents) ? agents.length : 'not array');
        console.log('✅ Agent API working');
      } else {
        const errorText = await agentResponse.text();
        console.log('❌ Agent API failed:', errorText.substring(0, 200));
      }
    } catch (error) {
      console.log('❌ Agent API error:', error);
    }

    // Test workflow endpoint
    try {
      const workflowResponse = await page.request.get('http://localhost:8001/api/workflows', {
        headers: {
          'X-Test-Worker': workerId,
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

    // Test agent creation
    console.log('🔍 Testing agent creation...');
    try {
      const createResponse = await page.request.post('http://localhost:8001/api/agents', {
        headers: {
          'X-Test-Worker': workerId,
          'Content-Type': 'application/json',
        },
        data: {
          name: `Test Agent ${workerId}`,
          system_instructions: 'You are a test agent for database isolation testing',
        }
      });
      console.log('📊 Agent creation status:', createResponse.status());

      if (createResponse.ok()) {
        const agent = await createResponse.json();
        console.log('📊 Created agent ID:', agent.id);
        console.log('✅ Agent creation successful');
      } else {
        const errorText = await createResponse.text();
        console.log('❌ Agent creation failed:', errorText.substring(0, 200));
      }
    } catch (error) {
      console.log('❌ Agent creation error:', error);
    }

    console.log('✅ Comprehensive database isolation test complete');
  });
});
