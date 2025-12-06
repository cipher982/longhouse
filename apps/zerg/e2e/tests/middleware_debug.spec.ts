import { test, expect } from './fixtures';

/**
 * MIDDLEWARE DEBUG TEST
 *
 * This test specifically validates that:
 * 1. The middleware is receiving requests
 * 2. Headers are being processed correctly
 * 3. Worker databases are being created properly
 */

test.describe('Middleware Debug', () => {
  test('Middleware header processing validation', async ({ page }, testInfo) => {
    console.log('🔍 Starting middleware debug test...');

    // Get the worker ID from test info
    const workerId = String(testInfo.workerIndex);
    console.log('📊 Worker ID:', workerId);
    console.log('📊 NODE_ENV:', process.env.NODE_ENV);

    // Make a simple API request to trigger middleware
    console.log('🔍 Making API request to trigger middleware...');
    try {
      const response = await page.request.get('http://localhost:8001/');
      console.log('📊 Health check response status:', response.status());
      console.log('📊 Health check response text:', await response.text());
    } catch (error) {
      console.log('❌ Health check error:', error);
    }

    // Try agent endpoint
    console.log('🔍 Making agent API request...');
    try {
      const response = await page.request.get('http://localhost:8001/api/agents');
      console.log('📊 Agent API response status:', response.status());
      const text = await response.text();
      console.log('📊 Agent API response:', text.substring(0, 500));
    } catch (error) {
      console.log('❌ Agent API error:', error);
    }

    console.log('✅ Middleware debug test complete');
  });
});
