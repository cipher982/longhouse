import { test, expect } from './fixtures';

/**
 * COMPREHENSIVE DEBUG TEST
 *
 * This test systematically checks every aspect of the database isolation:
 * 1. Middleware activation
 * 2. Header processing
 * 3. Database creation
 * 4. Table initialization
 * 5. CRUD operations
 */

test.describe('Comprehensive Debug', () => {
  test('Complete system debug and diagnosis', async ({ page, backendUrl }, testInfo) => {
    console.log('🔍 Starting comprehensive debug test...');

    // Get the worker ID from test info
    const workerId = String(testInfo.workerIndex);
    console.log('📊 Worker ID:', workerId);
    console.log('📊 NODE_ENV:', process.env.NODE_ENV);

    // Use shared backend started by Playwright webServer
    console.log('📊 Backend URL:', backendUrl);
    // No per-test server lifecycle required

    try {

    // Test 1: Basic connectivity
    console.log('🔍 Test 1: Basic connectivity');
    const response = await page.request.get(`${backendUrl}/`);
    expect(response.status()).toBe(200);
    console.log('✅ Backend is accessible');

    // Test 2: Header transmission
    console.log('🔍 Test 2: Header transmission');
    const headerResponse = await page.request.get(`${backendUrl}/`, {
      headers: {
        'X-Debug-Test': 'header-test'
      }
    });
    expect(headerResponse.status()).toBe(200);
    console.log('✅ Headers can be sent');

    // Test 3: Agent endpoint - GET (should work)
    console.log('🔍 Test 3: Agent GET endpoint');
    const agentGetResponse = await page.request.get(`${backendUrl}/api/agents`);
    expect(agentGetResponse.status()).toBe(200);
    const agents = await agentGetResponse.json();
    console.log('📊 Agent GET count:', agents.length);
    console.log('✅ Agent GET endpoint working');

    // Test 4: Different HTTP methods to test database
    console.log('🔍 Test 4: Testing different database operations');

    // Try a simpler POST endpoint first
    console.log('📊 Testing user endpoint...');
    const userResponse = await page.request.get(`${backendUrl}/api/users/me`);
    expect(userResponse.status()).toBe(200);
    const user = await userResponse.json();
    console.log('📊 User data available:', !!user);

    // Test 5: Try creating a workflow (with proper canvas)
    console.log('🔍 Test 5: Workflow creation test');
    const workflowResponse = await page.request.post(`${backendUrl}/api/workflows`, {
      headers: { 'Content-Type': 'application/json' },
      data: {
        name: `Test Workflow ${workerId}`,
        description: 'Test workflow for debugging',
        canvas: { nodes: [], edges: [] }
      }
    });
    expect(workflowResponse.status()).toBe(201);
    const workflow = await workflowResponse.json();
    console.log('📊 Workflow created ID:', workflow.id);
    console.log('✅ Workflow creation working');

    // Test 6: Agent creation with minimal data
    console.log('🔍 Test 6: Minimal agent creation');
    const agentResponse = await page.request.post(`${backendUrl}/api/agents`, {
      headers: { 'Content-Type': 'application/json' },
      data: {
        name: 'Minimal Agent',
        system_instructions: 'System instructions',
        task_instructions: 'Task instructions',
        model: 'gpt-mock', // Use mock model to avoid external dependencies
      }
    });
    expect(agentResponse.status()).toBe(201);
    const agent = await agentResponse.json();
    console.log('📊 Minimal agent created ID:', agent.id);
    console.log('✅ Agent creation working with mock model');

    // Test 7: System health
    console.log('🔍 Test 7: System health');
    const adminResponse = await page.request.get(`${backendUrl}/api/system/health`);
    expect(adminResponse.status()).toBe(200);
    const health = await adminResponse.json();
    console.log('📊 System health:', JSON.stringify(health));

    console.log('✅ Comprehensive debug test complete');

    } finally {
      // No cleanup necessary for shared backend
    }
  });
});
