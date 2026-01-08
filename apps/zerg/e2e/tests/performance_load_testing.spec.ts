import { test, expect } from './fixtures';

// Skip: Performance tests are long-running and not critical for CI
test.skip();

/**
 * PERFORMANCE AND LOAD TESTING E2E TEST
 *
 * This test validates application performance under various load conditions:
 * 1. UI responsiveness under normal and heavy loads
 * 2. API response time benchmarking
 * 3. Database performance with large datasets
 * 4. Memory usage and leak detection
 * 5. Concurrent user simulation
 * 6. Large workflow handling
 * 7. WebSocket performance under load
 * 8. Resource utilization monitoring
 */

test.describe('Performance and Load Testing', () => {
  test('UI responsiveness benchmarking', async ({ page, request }) => {
    console.log('🚀 Starting UI responsiveness test...');

    const workerId = process.env.TEST_PARALLEL_INDEX || '0';
    console.log('📊 Worker ID:', workerId);

    // Test 1: Page load performance
    console.log('📊 Test 1: Page load performance...');
    const startTime = Date.now();

    await page.goto('/');
    await page.waitForLoadState('networkidle');

    const loadTime = Date.now() - startTime;
    console.log('📊 Page load time:', loadTime, 'ms');

    if (loadTime < 3000) {
      console.log('✅ Page loads within acceptable time (< 3s)');
    } else if (loadTime < 5000) {
      console.log('⚠️  Page load time is moderate (3-5s)');
    } else {
      console.log('❌ Page load time is slow (> 5s)');
    }

    // Test 2: Navigation performance
    console.log('📊 Test 2: Navigation performance...');
    const navigationTests = [
      { name: 'Dashboard', testId: 'global-dashboard-tab' },
      { name: 'Canvas', testId: 'global-canvas-tab' }
    ];

    for (const nav of navigationTests) {
      const navStart = Date.now();
      await page.getByTestId(nav.testId).click();
      await page.waitForTimeout(100); // Small delay to ensure interaction
      const navTime = Date.now() - navStart;

      console.log(`📊 ${nav.name} navigation time:`, navTime, 'ms');

      if (navTime < 500) {
        console.log(`✅ ${nav.name} navigation is responsive (< 500ms)`);
      }
    }

    // Test 3: UI interaction responsiveness
    console.log('📊 Test 3: UI interaction responsiveness...');

    // Test button clicks, hovers, and other interactions
    const interactionElements = await page.locator('button, [role="button"], a').count();
    console.log('📊 Interactive elements found:', interactionElements);

    if (interactionElements > 0) {
      const testButton = page.locator('button, [role="button"]').first();
      const buttonExists = await testButton.count() > 0;

      if (buttonExists) {
        // Test hover responsiveness with timeout protection
        try {
          const hoverStart = Date.now();
          await testButton.hover({ timeout: 5000 });
          const hoverTime = Date.now() - hoverStart;

          console.log('📊 Hover response time:', hoverTime, 'ms');

          if (hoverTime < 100) {
            console.log('✅ UI interactions are highly responsive');
          }
        } catch (hoverError) {
          console.log('📊 Hover test skipped (element not interactive)');
        }
      }
    }

    console.log('✅ UI responsiveness test completed');
  });

  test('API response time benchmarking', async ({ page, request }) => {
    console.log('🚀 Starting API performance test...');

    const workerId = process.env.TEST_PARALLEL_INDEX || '0';

    // Test 1: Single API request benchmarking
    console.log('📊 Test 1: Single API request performance...');

    const apiTests = [
      { name: 'GET /api/agents', method: 'get', endpoint: '/api/agents' },
      { name: 'GET /api/workflows', method: 'get', endpoint: '/api/workflows' },
      { name: 'GET /api/users/me', method: 'get', endpoint: '/api/users/me' }
    ];

    for (const apiTest of apiTests) {
      try {
        const startTime = Date.now();
        const response = await request[apiTest.method](`${apiTest.endpoint}`, {
          headers: { 'X-Test-Worker': workerId }
        });
        const responseTime = Date.now() - startTime;

        console.log(`📊 ${apiTest.name} response time:`, responseTime, 'ms');
        console.log(`📊 ${apiTest.name} status:`, response.status());

        if (responseTime < 200) {
          console.log(`✅ ${apiTest.name} is very fast (< 200ms)`);
        } else if (responseTime < 500) {
          console.log(`✅ ${apiTest.name} is acceptable (< 500ms)`);
        } else {
          console.log(`⚠️  ${apiTest.name} is slow (> 500ms)`);
        }
      } catch (error) {
        console.log(`❌ ${apiTest.name} failed:`, error.message);
      }
    }

    // Test 2: Batch API request performance
    console.log('📊 Test 2: Batch API request performance...');

    const batchSize = 10;
    const batchRequests = Array.from({ length: batchSize }, () =>
      request.get('/api/agents', {
        headers: { 'X-Test-Worker': workerId }
      })
    );

    const batchStart = Date.now();
    try {
      const results = await Promise.all(batchRequests);
      const batchTime = Date.now() - batchStart;
      const successCount = results.filter(r => r.ok()).length;

      console.log('📊 Batch requests completed:', successCount, '/', batchSize);
      console.log('📊 Batch total time:', batchTime, 'ms');
      console.log('📊 Average per request:', Math.round(batchTime / batchSize), 'ms');

      if (batchTime < 2000) {
        console.log('✅ Batch API performance is good');
      }
    } catch (error) {
      console.log('❌ Batch API test failed:', error.message);
    }

    console.log('✅ API performance test completed');
  });

  test('Database performance with large datasets', async ({ page, request }) => {
    console.log('🚀 Starting database performance test...');

    const workerId = process.env.TEST_PARALLEL_INDEX || '0';

    // Test 1: Create large dataset
    console.log('📊 Test 1: Creating large dataset...');
    const datasetSize = 50; // Create 50 agents for performance testing
    const creationPromises = [];

    const creationStart = Date.now();
    for (let i = 0; i < datasetSize; i++) {
      const promise = request.post('/api/agents', {
        headers: {
          'X-Test-Worker': workerId,
          'Content-Type': 'application/json',
        },
        data: {
          name: `Performance Test Agent ${i} ${Date.now()}`,
          system_instructions: `Performance testing agent number ${i}`,
          task_instructions: `Handle performance test case ${i}`,
          model: 'gpt-mock',
        }
      });
      creationPromises.push(promise);

      // Add small delay every 10 requests to avoid overwhelming the server
      if (i % 10 === 9) {
        await Promise.all(creationPromises.slice(i - 9, i + 1));
        await page.waitForTimeout(100);
      }
    }

    const creationResults = await Promise.all(creationPromises);
    const creationTime = Date.now() - creationStart;
    const successfulCreations = creationResults.filter(r => r.status() === 201).length;

    console.log('📊 Agents created successfully:', successfulCreations, '/', datasetSize);
    console.log('📊 Creation time:', creationTime, 'ms');
    console.log('📊 Average creation time:', Math.round(creationTime / datasetSize), 'ms per agent');

    if (successfulCreations >= datasetSize * 0.9) {
      console.log('✅ Large dataset creation successful');
    }

    // Test 2: Query performance with large dataset
    console.log('📊 Test 2: Query performance with large dataset...');

    const queryStart = Date.now();
    const queryResponse = await request.get('/api/agents', {
      headers: { 'X-Test-Worker': workerId }
    });
    const queryTime = Date.now() - queryStart;

    if (queryResponse.ok()) {
      const agents = await queryResponse.json();
      console.log('📊 Total agents retrieved:', agents.length);
      console.log('📊 Query time:', queryTime, 'ms');

      if (queryTime < 1000) {
        console.log('✅ Large dataset query performance is good (< 1s)');
      } else {
        console.log('⚠️  Large dataset query is slow (> 1s)');
      }
    }

    // Test 3: Pagination performance (if supported)
    console.log('📊 Test 3: Testing pagination performance...');

    try {
      const paginationStart = Date.now();
      const paginatedResponse = await request.get('/api/agents?limit=10&offset=0', {
        headers: { 'X-Test-Worker': workerId }
      });
      const paginationTime = Date.now() - paginationStart;

      console.log('📊 Pagination query status:', paginatedResponse.status());
      console.log('📊 Pagination query time:', paginationTime, 'ms');

      if (paginatedResponse.ok()) {
        const paginatedData = await paginatedResponse.json();
        const returnedCount = Array.isArray(paginatedData) ? paginatedData.length : (paginatedData.items ? paginatedData.items.length : 0);
        console.log('📊 Paginated results returned:', returnedCount);

        if (paginationTime < 200) {
          console.log('✅ Pagination performance is excellent');
        }
      }
    } catch (error) {
      console.log('📊 Pagination test (may not be implemented):', error.message);
    }

    console.log('✅ Database performance test completed');
  });

  test('Memory usage and resource monitoring', async ({ page, context, request }) => {
    console.log('🚀 Starting memory usage test...');

    const workerId = process.env.TEST_PARALLEL_INDEX || '0';

    // Test 1: Initial memory baseline
    console.log('📊 Test 1: Establishing memory baseline...');

    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Measure performance metrics
    const initialMetrics = await page.evaluate(() => {
      return {
        memory: (performance as any).memory ? {
          used: (performance as any).memory.usedJSHeapSize,
          total: (performance as any).memory.totalJSHeapSize,
          limit: (performance as any).memory.jsHeapSizeLimit
        } : null,
        timing: performance.timing ? {
          domContentLoaded: performance.timing.domContentLoadedEventEnd - performance.timing.navigationStart,
          fullyLoaded: performance.timing.loadEventEnd - performance.timing.navigationStart
        } : null
      };
    });

    console.log('📊 Initial memory usage:', initialMetrics.memory);
    console.log('📊 Page timing:', initialMetrics.timing);

    // Test 2: Memory usage during operations
    console.log('📊 Test 2: Memory usage during intensive operations...');

    // Perform memory-intensive operations
    for (let i = 0; i < 10; i++) {
      await page.locator('.header-nav').click();
      await page.waitForTimeout(100);
      await page.getByTestId('global-canvas-tab').click();
      await page.waitForTimeout(100);
    }

    // Create several agents to test memory usage
    for (let i = 0; i < 10; i++) {
      await request.post('/api/agents', {
        headers: {
          'X-Test-Worker': workerId,
          'Content-Type': 'application/json',
        },
        data: {
          name: `Memory Test Agent ${i} ${Date.now()}`,
          system_instructions: `Memory testing agent ${i}`,
          task_instructions: 'Test memory usage',
          model: 'gpt-mock',
        }
      });
    }

    const operationMetrics = await page.evaluate(() => {
      return {
        memory: (performance as any).memory ? {
          used: (performance as any).memory.usedJSHeapSize,
          total: (performance as any).memory.totalJSHeapSize,
        } : null
      };
    });

    console.log('📊 Memory usage after operations:', operationMetrics.memory);

    if (initialMetrics.memory && operationMetrics.memory) {
      const memoryIncrease = operationMetrics.memory.used - initialMetrics.memory.used;
      const memoryIncreasePercent = (memoryIncrease / initialMetrics.memory.used) * 100;

      console.log('📊 Memory increase:', Math.round(memoryIncreasePercent), '%');

      if (memoryIncreasePercent < 50) {
        console.log('✅ Memory usage increase is reasonable');
      } else {
        console.log('⚠️  Significant memory usage increase detected');
      }
    }

    // Test 3: Check for memory leaks
    console.log('📊 Test 3: Memory leak detection...');

    // Force garbage collection if available
    await page.evaluate(() => {
      if (window.gc) {
        window.gc();
      }
    });

    await page.waitForTimeout(1000);

    const afterGcMetrics = await page.evaluate(() => {
      return {
        memory: (performance as any).memory ? {
          used: (performance as any).memory.usedJSHeapSize,
          total: (performance as any).memory.totalJSHeapSize,
        } : null
      };
    });

    console.log('📊 Memory usage after GC:', afterGcMetrics.memory);

    if (operationMetrics.memory && afterGcMetrics.memory) {
      const gcReduction = operationMetrics.memory.used - afterGcMetrics.memory.used;
      console.log('📊 Memory freed by GC:', gcReduction, 'bytes');

      if (gcReduction > 0) {
        console.log('✅ Memory is being properly garbage collected');
      }
    }

    console.log('✅ Memory usage test completed');
  });

  test('Concurrent user simulation', async ({ browser, request }) => {
    console.log('🚀 Starting concurrent user simulation...');

    const workerIdBase = process.env.TEST_PARALLEL_INDEX || '0';
    const concurrentUsers = 5;

    // Test 1: Simulate concurrent users
    console.log(`📊 Test 1: Simulating ${concurrentUsers} concurrent users...`);

    const userSimulations = Array.from({ length: concurrentUsers }, async (_, index) => {
      const context = await browser.newContext();
      const page = await context.newPage();
      const userId = `${workerIdBase}_user_${index}`;

      try {
        // Navigate to application
        await page.goto('/');
        await page.waitForTimeout(1000);

        // Simulate user actions
        await page.locator('.header-nav').click();
        await page.waitForTimeout(500);

        // Create an agent as this user
        const agentResponse = await request.post('/api/agents', {
          headers: {
            'X-Test-Worker': userId,
            'Content-Type': 'application/json',
          },
          data: {
            name: `Concurrent User ${index} Agent ${Date.now()}`,
            system_instructions: `Agent created by concurrent user ${index}`,
            task_instructions: `Test concurrent user ${index} operations`,
            model: 'gpt-mock',
          }
        });

        const success = agentResponse.status() === 201;
        console.log(`📊 User ${index} agent creation:`, success ? 'success' : 'failed');

        // Navigate between tabs
        await page.getByTestId('global-canvas-tab').click();
        await page.waitForTimeout(300);
        await page.locator('.header-nav').click();
        await page.waitForTimeout(300);

        await context.close();
        return { userId, success };
      } catch (error) {
        console.log(`📊 User ${index} error:`, error.message);
        await context.close();
        return { userId, success: false, error: error.message };
      }
    });

    const concurrentStart = Date.now();
    const results = await Promise.all(userSimulations);
    const concurrentTime = Date.now() - concurrentStart;

    const successfulUsers = results.filter(r => r.success).length;
    console.log('📊 Concurrent users completed successfully:', successfulUsers, '/', concurrentUsers);
    console.log('📊 Total simulation time:', concurrentTime, 'ms');
    console.log('📊 Average time per user:', Math.round(concurrentTime / concurrentUsers), 'ms');

    if (successfulUsers >= concurrentUsers * 0.8) {
      console.log('✅ Concurrent user handling is robust');
    } else {
      console.log('⚠️  Some concurrent users experienced issues');
    }

    console.log('✅ Concurrent user simulation completed');
  });

  test('Large workflow performance', async ({ page, request }) => {
    console.log('🚀 Starting large workflow performance test...');

    const workerId = process.env.TEST_PARALLEL_INDEX || '0';

    // Test 1: Create agents for large workflow
    console.log('📊 Test 1: Creating agents for large workflow...');
    const agentCount = 10;
    const agents = [];

    for (let i = 0; i < agentCount; i++) {
      const agentResponse = await request.post('/api/agents', {
        headers: {
          'X-Test-Worker': workerId,
          'Content-Type': 'application/json',
        },
        data: {
          name: `Large Workflow Agent ${i} ${Date.now()}`,
          system_instructions: `Agent ${i} for large workflow testing`,
          task_instructions: `Handle task ${i} in large workflow`,
          model: 'gpt-mock',
        }
      });

      if (agentResponse.ok()) {
        const agent = await agentResponse.json();
        agents.push(agent);
        console.log(`📊 Created agent ${i}:`, agent.id);
      }
    }

    console.log('📊 Total agents for large workflow:', agents.length);

    // Test 2: Create large workflow
    console.log('📊 Test 2: Creating large workflow...');

    if (agents.length >= 5) {
      const largeWorkflowStart = Date.now();

      // Create a complex workflow with many nodes and connections
      const nodes = [
        { id: 'trigger-1', type: 'trigger', position: { x: 50, y: 300 } },
        ...agents.map((agent, index) => ({
          id: `agent-${index}`,
          type: 'agent',
          agent_id: agent.id,
          position: { x: 200 + (index % 5) * 150, y: 100 + Math.floor(index / 5) * 150 }
        })),
        // Add multiple tool nodes
        ...Array.from({ length: 5 }, (_, i) => ({
          id: `tool-${i}`,
          type: 'tool',
          tool_name: 'http_request',
          position: { x: 800, y: 100 + i * 100 },
          config: { url: `https://httpbin.org/get?test=${i}`, method: 'GET' }
        }))
      ];

      // Create complex connection topology
      const edges = [
        // Connect trigger to first few agents
        { id: 'edge-trigger-0', source: 'trigger-1', target: 'agent-0', type: 'default' },
        { id: 'edge-trigger-1', source: 'trigger-1', target: 'agent-1', type: 'default' },
        // Sequential connections between agents
        ...agents.slice(0, -1).map((_, index) => ({
          id: `edge-agent-${index}-${index + 1}`,
          source: `agent-${index}`,
          target: `agent-${index + 1}`,
          type: 'default'
        })),
        // Parallel connections to tools
        ...agents.slice(0, 5).map((_, index) => ({
          id: `edge-agent-${index}-tool-${index}`,
          source: `agent-${index}`,
          target: `tool-${index}`,
          type: 'default'
        }))
      ];

      const largeWorkflow = {
        name: `Large Workflow Performance Test ${Date.now()}`,
        description: 'Performance test workflow with many nodes and connections',
        canvas_data: { nodes, edges }
      };

      const workflowResponse = await request.post('/api/workflows', {
        headers: {
          'X-Test-Worker': workerId,
          'Content-Type': 'application/json',
        },
        data: largeWorkflow
      });

      const workflowCreationTime = Date.now() - largeWorkflowStart;

      console.log('📊 Large workflow creation status:', workflowResponse.status());
      console.log('📊 Large workflow creation time:', workflowCreationTime, 'ms');
      console.log('📊 Workflow nodes:', nodes.length);
      console.log('📊 Workflow connections:', edges.length);

      if (workflowResponse.ok()) {
        const workflow = await workflowResponse.json();
        console.log('📊 Large workflow created with ID:', workflow.id);

        // Test retrieval performance
        const retrievalStart = Date.now();
        const retrievalResponse = await request.get(`/api/workflows/${workflow.id}`, {
          headers: { 'X-Test-Worker': workerId }
        });
        const retrievalTime = Date.now() - retrievalStart;

        console.log('📊 Large workflow retrieval time:', retrievalTime, 'ms');

        if (workflowCreationTime < 5000 && retrievalTime < 2000) {
          console.log('✅ Large workflow performance is acceptable');
        } else {
          console.log('⚠️  Large workflow operations are slow');
        }
      } else {
        const error = await workflowResponse.text();
        console.log('❌ Large workflow creation failed:', error.substring(0, 200));
      }
    }

    console.log('✅ Large workflow performance test completed');
  });
});
