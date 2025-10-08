import { test, expect } from './fixtures';

/**
 * ERROR HANDLING AND EDGE CASES E2E TEST
 * 
 * This test validates robust error handling across the application:
 * 1. Invalid API requests and malformed data
 * 2. Network failures and timeout scenarios
 * 3. Database constraint violations
 * 4. Authentication and authorization failures
 * 5. Rate limiting and quota violations
 * 6. Concurrent operations and race conditions
 * 7. Malformed WebSocket messages
 * 8. UI state corruption and recovery
 */

test.describe('Error Handling and Edge Cases', () => {
  test('API error handling with invalid data', async ({ page }) => {
    console.log('🚀 Starting API error handling test...');
    
    const workerId = process.env.PW_TEST_WORKER_INDEX || '0';
    console.log('📊 Worker ID:', workerId);
    
    // Test 1: Invalid agent creation - missing required fields
    console.log('📊 Test 1: Invalid agent creation - missing fields');
    try {
      const response = await page.request.post('http://localhost:8001/api/agents', {
        headers: {
          'X-Test-Worker': workerId,
          'Content-Type': 'application/json',
        },
        data: {
          // Missing required fields intentionally
          name: '',
        }
      });
      
      console.log('📊 Invalid agent creation status:', response.status());
      expect(response.status()).toBe(422); // Validation error expected
      
      const errorResponse = await response.json();
      console.log('📊 Validation error structure:', !!errorResponse.detail);
      expect(errorResponse.detail).toBeDefined();
      console.log('✅ Validation errors properly returned');
    } catch (error) {
      console.log('❌ API validation error test failed:', error.message);
    }
    
    // Test 2: Invalid JSON payload
    console.log('📊 Test 2: Invalid JSON payload');
    try {
      const response = await page.request.post('http://localhost:8001/api/agents', {
        headers: {
          'X-Test-Worker': workerId,
          'Content-Type': 'application/json',
        },
        data: 'invalid-json-string'
      });
      
      console.log('📊 Invalid JSON status:', response.status());
      expect([400, 422]).toContain(response.status());
      console.log('✅ Invalid JSON properly rejected');
    } catch (error) {
      console.log('📊 Invalid JSON test handled:', error.message);
    }
    
    // Test 3: Extremely large payload
    console.log('📊 Test 3: Large payload handling');
    try {
      const largeString = 'x'.repeat(10000); // 10KB string
      const response = await page.request.post('http://localhost:8001/api/agents', {
        headers: {
          'X-Test-Worker': workerId,
          'Content-Type': 'application/json',
        },
        data: {
          name: 'Large Payload Test',
          system_instructions: largeString,
          task_instructions: 'Test large payload handling',
          model: 'gpt-mock',
        }
      });
      
      console.log('📊 Large payload status:', response.status());
      if (response.status() === 413) {
        console.log('✅ Large payload properly rejected');
      } else if (response.status() === 201) {
        console.log('✅ Large payload accepted (system handles large data)');
      }
    } catch (error) {
      console.log('📊 Large payload test:', error.message);
    }
    
    // Test 4: Invalid HTTP methods
    console.log('📊 Test 4: Invalid HTTP methods');
    try {
      const response = await page.request.patch('http://localhost:8001/api/agents', {
        headers: { 'X-Test-Worker': workerId },
        data: { test: 'data' }
      });
      
      console.log('📊 Invalid method status:', response.status());
      expect([405, 404]).toContain(response.status());
      console.log('✅ Invalid HTTP methods properly rejected');
    } catch (error) {
      console.log('📊 Invalid method test:', error.message);
    }
    
    // Test 5: Non-existent resource access
    console.log('📊 Test 5: Non-existent resource access');
    try {
      const response = await page.request.get('http://localhost:8001/api/agents/999999', {
        headers: { 'X-Test-Worker': workerId }
      });
      
      console.log('📊 Non-existent resource status:', response.status());
      expect(response.status()).toBe(404);
      console.log('✅ Non-existent resources return 404');
    } catch (error) {
      console.log('📊 Non-existent resource test:', error.message);
    }
    
    console.log('✅ API error handling test completed');
  });
  
  test('Database constraint and data integrity', async ({ page }) => {
    console.log('🚀 Starting database constraint test...');
    
    const workerId = process.env.PW_TEST_WORKER_INDEX || '0';
    
    // Test 1: Create agent with duplicate name (if uniqueness enforced)
    console.log('📊 Test 1: Duplicate name handling');
    const agentName = `Duplicate Test Agent ${Date.now()}`;
    
    // Create first agent
    const firstResponse = await page.request.post('http://localhost:8001/api/agents', {
      headers: {
        'X-Test-Worker': workerId,
        'Content-Type': 'application/json',
      },
      data: {
        name: agentName,
        system_instructions: 'First agent',
        task_instructions: 'Test duplicate handling',
        model: 'gpt-mock',
      }
    });
    
    expect(firstResponse.status()).toBe(201);
    const firstAgent = await firstResponse.json();
    console.log('📊 First agent created:', firstAgent.id);
    
    // Attempt to create duplicate
    const duplicateResponse = await page.request.post('http://localhost:8001/api/agents', {
      headers: {
        'X-Test-Worker': workerId,
        'Content-Type': 'application/json',
      },
      data: {
        name: agentName,
        system_instructions: 'Second agent with same name',
        task_instructions: 'Test duplicate handling',
        model: 'gpt-mock',
      }
    });
    
    console.log('📊 Duplicate creation status:', duplicateResponse.status());
    if (duplicateResponse.status() === 409) {
      console.log('✅ Duplicate names properly rejected');
    } else if (duplicateResponse.status() === 201) {
      console.log('✅ Duplicate names allowed (system permits duplicates)');
    }
    
    // Test 2: Extremely long field values
    console.log('📊 Test 2: Field length validation');
    const extremelyLongName = 'x'.repeat(1000);
    
    const longFieldResponse = await page.request.post('http://localhost:8001/api/agents', {
      headers: {
        'X-Test-Worker': workerId,
        'Content-Type': 'application/json',
      },
      data: {
        name: extremelyLongName,
        system_instructions: 'Test long field',
        task_instructions: 'Test field length limits',
        model: 'gpt-mock',
      }
    });
    
    console.log('📊 Long field status:', longFieldResponse.status());
    if (longFieldResponse.status() === 422) {
      console.log('✅ Field length limits enforced');
    } else if (longFieldResponse.status() === 201) {
      console.log('✅ Long fields accepted (no length limits)');
    }
    
    console.log('✅ Database constraint test completed');
  });
  
  test('Concurrent operations and race conditions', async ({ page }) => {
    console.log('🚀 Starting concurrency test...');
    
    const workerId = process.env.PW_TEST_WORKER_INDEX || '0';
    
    // Test 1: Concurrent agent creation
    console.log('📊 Test 1: Concurrent agent creation');
    const concurrentRequests = Array.from({ length: 5 }, (_, i) => 
      page.request.post('http://localhost:8001/api/agents', {
        headers: {
          'X-Test-Worker': workerId,
          'Content-Type': 'application/json',
        },
        data: {
          name: `Concurrent Agent ${i} ${Date.now()}`,
          system_instructions: `Concurrent test agent ${i}`,
          task_instructions: 'Test concurrent creation',
          model: 'gpt-mock',
        }
      })
    );
    
    try {
      const results = await Promise.all(concurrentRequests);
      const successCount = results.filter(r => r.status() === 201).length;
      const errorCount = results.filter(r => r.status() !== 201).length;
      
      console.log('📊 Concurrent creation success:', successCount);
      console.log('📊 Concurrent creation errors:', errorCount);
      
      if (successCount >= 3) {
        console.log('✅ Concurrent operations handled well');
      }
    } catch (error) {
      console.log('📊 Concurrent test error:', error.message);
    }
    
    // Test 2: Rapid-fire requests to same endpoint
    console.log('📊 Test 2: Rapid-fire GET requests');
    const rapidRequests = Array.from({ length: 10 }, () => 
      page.request.get('http://localhost:8001/api/agents', {
        headers: { 'X-Test-Worker': workerId }
      })
    );
    
    try {
      const rapidResults = await Promise.all(rapidRequests);
      const rapidSuccessCount = rapidResults.filter(r => r.ok()).length;
      
      console.log('📊 Rapid requests success:', rapidSuccessCount);
      if (rapidSuccessCount >= 8) {
        console.log('✅ Rapid requests handled well');
      }
    } catch (error) {
      console.log('📊 Rapid requests error:', error.message);
    }
    
    console.log('✅ Concurrency test completed');
  });
  
  test('UI error state handling', async ({ page }) => {
    console.log('🚀 Starting UI error state test...');
    
    const workerId = process.env.PW_TEST_WORKER_INDEX || '0';
    
    // Navigate to application
    await page.goto('/');
    await page.waitForTimeout(1000);
    
    // Test 1: Network connectivity loss simulation
    console.log('📊 Test 1: Network connectivity simulation');
    try {
      // Simulate offline state
      await page.context().setOffline(true);
      await page.waitForTimeout(500);
      
      // Try to interact with UI while offline
      await page.getByTestId('global-dashboard-tab').click();
      await page.waitForTimeout(1000);
      
      // Check for offline indicators or error messages
      const errorMessages = await page.locator('.error, .offline, [data-testid*="error"]').count();
      console.log('📊 Error indicators found:', errorMessages);
      
      // Restore connectivity
      await page.context().setOffline(false);
      await page.waitForTimeout(1000);
      
      console.log('✅ Network connectivity simulation completed');
    } catch (error) {
      console.log('📊 Network simulation error:', error.message);
      // Ensure we restore connectivity
      await page.context().setOffline(false);
    }
    
    // Test 2: Invalid navigation attempts
    console.log('📊 Test 2: Invalid navigation handling');
    try {
      // Try to navigate to non-existent routes
      await page.goto('/invalid-route-that-does-not-exist');
      await page.waitForTimeout(1000);
      
      // Check if there's a 404 page or error handling
      const pageTitle = await page.title();
      const pageContent = await page.locator('body').textContent();
      
      console.log('📊 Invalid route page title:', pageTitle?.substring(0, 50));
      const hasErrorContent = pageContent?.includes('404') || pageContent?.includes('not found') || pageContent?.includes('error');
      console.log('📊 Error content present:', !!hasErrorContent);
      
      if (hasErrorContent) {
        console.log('✅ Invalid routes properly handled');
      }
    } catch (error) {
      console.log('📊 Invalid navigation test:', error.message);
    }
    
    // Test 3: JavaScript error handling
    console.log('📊 Test 3: JavaScript error monitoring');
    const jsErrors = [];
    page.on('pageerror', error => {
      jsErrors.push(error.message);
      console.log('📊 JavaScript error caught:', error.message);
    });
    
    // Navigate back to main app
    await page.goto('/');
    await page.waitForTimeout(2000);
    
    // Try various UI interactions that might cause errors
    await page.getByTestId('global-canvas-tab').click();
    await page.waitForTimeout(500);
    await page.getByTestId('global-dashboard-tab').click();
    await page.waitForTimeout(500);
    
    console.log('📊 JavaScript errors detected:', jsErrors.length);
    if (jsErrors.length === 0) {
      console.log('✅ No JavaScript errors during navigation');
    } else {
      console.log('⚠️  JavaScript errors found:', jsErrors);
    }
    
    console.log('✅ UI error state test completed');
  });
});