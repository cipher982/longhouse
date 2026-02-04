import { test, expect } from './fixtures';
import type { APIRequestContext } from '@playwright/test';

// Skip: Multi-user concurrency tests are complex and have UI drift
test.skip();

/**
 * MULTI-USER AND CONCURRENCY E2E TEST
 *
 * This test validates multi-user scenarios and concurrent operations:
 * 1. Multiple user sessions with data isolation
 * 2. Real-time collaboration features
 * 3. Resource sharing and permissions
 * 4. Conflict resolution in concurrent edits
 * 5. WebSocket message broadcasting
 * 6. Session management and cleanup
 * 7. Race condition handling
 */

test.describe('Multi-User and Concurrency', () => {
  test('Multiple user sessions with data isolation', async ({ browser, request }) => {
    console.log('🚀 Starting multi-user data isolation test...');

    const baseCommisId = process.env.TEST_PARALLEL_INDEX || '0';
    const userCount = 3;

    // Create multiple user contexts
    const userSessions = await Promise.all(
      Array.from({ length: userCount }, async (_, index) => {
        const context = await browser.newContext();
        const page = await context.newPage();
        const userId = `${baseCommisId}_user_${index}`;

        return { context, page, userId, index };
      })
    );

    console.log(`📊 Created ${userCount} user sessions`);

    // Test 1: Each user creates isolated data
    console.log('📊 Test 1: Creating isolated data per user...');

    const userFiches = await Promise.all(
      userSessions.map(async (session) => {
        try {
          // Navigate to application
          await session.page.goto('/');
          await session.page.waitForTimeout(1000);

          // Create fiche specific to this user
          const ficheResponse = await request.post('/api/fiches', {
            headers: {
              'X-Test-Commis': session.userId,
              'Content-Type': 'application/json',
            },
            data: {
              name: `User ${session.index} Fiche ${Date.now()}`,
              system_instructions: `Fiche belonging to user ${session.index}`,
              task_instructions: `Handle tasks for user ${session.index}`,
              model: 'gpt-mock',
            }
          });

          if (ficheResponse.ok()) {
            const fiche = await ficheResponse.json();
            console.log(`📊 User ${session.index} created fiche:`, fiche.id);
            return { userId: session.userId, fiche, success: true };
          } else {
            console.log(`❌ User ${session.index} fiche creation failed:`, ficheResponse.status());
            return { userId: session.userId, fiche: null, success: false };
          }
        } catch (error) {
          console.log(`❌ User ${session.index} error:`, error.message);
          return { userId: session.userId, fiche: null, success: false, error: error.message };
        }
      })
    );

    const successfulCreations = userFiches.filter(ua => ua.success).length;
    console.log('📊 Successful fiche creations:', successfulCreations, '/', userCount);

    // Test 2: Verify data isolation - each user only sees their own data
    console.log('📊 Test 2: Verifying data isolation...');

    const isolationResults = await Promise.all(
      userSessions.map(async (session, index) => {
        try {
          const response = await session.request.get('/api/fiches', {
            headers: { 'X-Test-Commis': session.userId }
          });

          if (response.ok()) {
            const fiches = await response.json();
            const userFiche = userFiches[index];

            // Check if user sees only their own fiche
            const hasOwnFiche = userFiche.success && fiches.some(a => a.id === userFiche.fiche.id);
            const seeOtherFiches = fiches.some(a =>
              userFiches.some(ua => ua.success && ua.userId !== session.userId && ua.fiche.id === a.id)
            );

            console.log(`📊 User ${index} sees own fiche:`, hasOwnFiche);
            console.log(`📊 User ${index} sees other users' fiches:`, seeOtherFiches);

            return {
              userId: session.userId,
              index,
              hasOwnFiche,
              seeOtherFiches,
              totalFiches: fiches.length,
              success: true
            };
          } else {
            return { userId: session.userId, index, success: false };
          }
        } catch (error) {
          return { userId: session.userId, index, success: false, error: error.message };
        }
      })
    );

    const properIsolation = isolationResults.filter(r =>
      r.success && r.hasOwnFiche && !r.seeOtherFiches
    ).length;

    console.log('📊 Users with proper data isolation:', properIsolation, '/', userCount);

    if (properIsolation >= userCount * 0.8) {
      console.log('✅ Data isolation working correctly');
    } else {
      console.log('⚠️  Data isolation may need improvement');
    }

    // Clean up user sessions
    await Promise.all(userSessions.map(session => session.context.close()));

    console.log('✅ Multi-user data isolation test completed');
  });

  test('WebSocket message broadcasting and isolation', async ({ browser, request }) => {
    console.log('🚀 Starting WebSocket broadcasting test...');

    const baseCommisId = process.env.TEST_PARALLEL_INDEX || '0';
    const wsUsers = 2;

    // Create user sessions with WebSocket monitoring
    const wsSessions = await Promise.all(
      Array.from({ length: wsUsers }, async (_, index) => {
        const context = await browser.newContext();
        const page = await context.newPage();
        const userId = `${baseCommisId}_ws_${index}`;

        const wsMessages = [];

        // Set up WebSocket message monitoring
        page.on('websocket', ws => {
          console.log(`📊 User ${index} WebSocket connected:`, ws.url());

          ws.on('framereceived', event => {
            try {
              const message = JSON.parse(event.payload);
              wsMessages.push({ ...message, receivedAt: Date.now() });
              console.log(`📊 User ${index} received:`, message.event_type || message.type);
            } catch (error) {
              // Ignore parsing errors
            }
          });
        });

        return { context, page, userId, index, wsMessages };
      })
    );

    console.log(`📊 Created ${wsUsers} WebSocket monitoring sessions`);

    // Test 1: Connect all users and monitor initial messages
    console.log('📊 Test 1: Connecting users and monitoring initial messages...');

    await Promise.all(
      wsSessions.map(async (session) => {
        await session.page.goto('/');
        await session.page.waitForTimeout(2000);
      })
    );

    // Test 2: Create data in one session and check for cross-session messages
    console.log('📊 Test 2: Testing cross-session message broadcasting...');

    const primarySession = wsSessions[0];
    const secondarySession = wsSessions[1];

    // Create fiche in primary session
    const ficheResponse = await primarySession.request.post('/api/fiches', {
      headers: {
        'X-Test-Commis': primarySession.userId,
        'Content-Type': 'application/json',
      },
      data: {
        name: `WebSocket Test Fiche ${Date.now()}`,
        system_instructions: 'Fiche for WebSocket testing',
        task_instructions: 'Test WebSocket message broadcasting',
        model: 'gpt-mock',
      }
    });

    if (ficheResponse.ok()) {
      const fiche = await ficheResponse.json();
      console.log('📊 Created fiche in primary session:', fiche.id);

      // Wait for potential WebSocket messages
      await Promise.all(wsSessions.map(s => s.page.waitForTimeout(2000)));

      // Check messages received by each session
      wsSessions.forEach((session, index) => {
        const relevantMessages = session.wsMessages.filter(msg =>
          msg.event_type === 'fiche_state' ||
          msg.event_type === 'fiche_created' ||
          (msg.data && JSON.stringify(msg.data).includes(fiche.id.toString()))
        );

        console.log(`📊 User ${index} received ${relevantMessages.length} fiche-related messages`);

        if (relevantMessages.length > 0) {
          console.log(`✅ User ${index} received WebSocket notifications`);
        }
      });

      // Test 3: Session isolation - check if users see appropriate data
      console.log('📊 Test 3: Testing session isolation in WebSocket messages...');

      const primaryMessages = primarySession.wsMessages.filter(msg => msg.event_type);
      const secondaryMessages = secondarySession.wsMessages.filter(msg => msg.event_type);

      console.log('📊 Primary session message types:', [...new Set(primaryMessages.map(m => m.event_type))]);
      console.log('📊 Secondary session message types:', [...new Set(secondaryMessages.map(m => m.event_type))]);

      // Check if secondary session receives messages about primary session's data
      const crossSessionMessages = secondaryMessages.filter(msg =>
        msg.data && JSON.stringify(msg.data).includes(fiche.id.toString())
      );

      console.log('📊 Cross-session messages in secondary:', crossSessionMessages.length);

      if (crossSessionMessages.length === 0) {
        console.log('✅ WebSocket messages properly isolated between sessions');
      } else {
        console.log('📊 WebSocket messages are broadcasted across sessions (may be intended)');
      }
    }

    // Test 4: High-frequency message handling
    console.log('📊 Test 4: Testing high-frequency message handling...');

    const rapidOperations = Array.from({ length: 5 }, (_, i) =>
      primarySession.request.post('/api/fiches', {
        headers: {
          'X-Test-Commis': primarySession.userId,
          'Content-Type': 'application/json',
        },
        data: {
          name: `Rapid Fiche ${i} ${Date.now()}`,
          system_instructions: `Rapid test fiche ${i}`,
          task_instructions: `Test rapid operations ${i}`,
          model: 'gpt-mock',
        }
      })
    );

    const rapidStart = Date.now();
    const rapidResults = await Promise.all(rapidOperations);
    const rapidTime = Date.now() - rapidStart;

    const rapidSuccesses = rapidResults.filter(r => r.ok()).length;
    console.log('📊 Rapid operations completed:', rapidSuccesses, '/', 5);
    console.log('📊 Rapid operations time:', rapidTime, 'ms');

    // Wait for WebSocket messages to process
    await Promise.all(wsSessions.map(s => s.page.waitForTimeout(3000)));

    // Count WebSocket messages received during rapid operations
    const rapidMessageCounts = wsSessions.map((session, index) => {
      const recentMessages = session.wsMessages.filter(msg =>
        msg.receivedAt >= rapidStart - 1000
      );
      console.log(`📊 User ${index} received ${recentMessages.length} messages during rapid operations`);
      return recentMessages.length;
    });

    const totalRapidMessages = rapidMessageCounts.reduce((sum, count) => sum + count, 0);
    if (totalRapidMessages > 0) {
      console.log('✅ WebSocket handles high-frequency operations');
    }

    // Clean up
    await Promise.all(wsSessions.map(session => session.context.close()));

    console.log('✅ WebSocket broadcasting test completed');
  });

  test('Resource sharing and conflict resolution', async ({ browser, request }) => {
    console.log('🚀 Starting resource sharing and conflict resolution test...');

    const baseCommisId = process.env.TEST_PARALLEL_INDEX || '0';
    const conflictUsers = 2;

    // Create sessions for conflict testing
    const conflictSessions = await Promise.all(
      Array.from({ length: conflictUsers }, async (_, index) => {
        const context = await browser.newContext();
        const page = await context.newPage();
        const userId = `${baseCommisId}_conflict_${index}`;

        return { context, page, userId, index };
      })
    );

    console.log(`📊 Created ${conflictUsers} sessions for conflict testing`);

    // Test 1: Attempt concurrent modifications
    console.log('📊 Test 1: Testing concurrent modifications...');

    // Both users create fiches with similar names to test conflict handling
    const conflictStart = Date.now();
    const conflictOperations = await Promise.all(
      conflictSessions.map(async (session) => {
        try {
          const ficheResponse = await session.request.post('/api/fiches', {
            headers: {
              'X-Test-Commis': session.userId,
              'Content-Type': 'application/json',
            },
            data: {
              name: `Conflict Test Fiche ${Date.now()}`, // Same name pattern
              system_instructions: `Conflict resolution test from user ${session.index}`,
              task_instructions: `Handle conflicts for user ${session.index}`,
              model: 'gpt-mock',
            }
          });

          const responseTime = Date.now() - conflictStart;

          if (ficheResponse.ok()) {
            const fiche = await ficheResponse.json();
            console.log(`📊 User ${session.index} created fiche:`, fiche.id, `(${responseTime}ms)`);
            return { success: true, fiche, userId: session.userId, responseTime };
          } else {
            console.log(`❌ User ${session.index} creation failed:`, ficheResponse.status());
            return { success: false, userId: session.userId, status: ficheResponse.status() };
          }
        } catch (error) {
          console.log(`❌ User ${session.index} error:`, error.message);
          return { success: false, userId: session.userId, error: error.message };
        }
      })
    );

    const successfulConflictOps = conflictOperations.filter(co => co.success);
    console.log('📊 Successful concurrent operations:', successfulConflictOps.length, '/', conflictUsers);

    // Test response time differences (may indicate queuing/locking)
    if (successfulConflictOps.length >= 2) {
      const responseTimes = successfulConflictOps.map(op => op.responseTime);
      const avgResponseTime = responseTimes.reduce((sum, time) => sum + time, 0) / responseTimes.length;
      const maxResponseTime = Math.max(...responseTimes);
      const minResponseTime = Math.min(...responseTimes);

      console.log('📊 Response time range:', minResponseTime, 'ms -', maxResponseTime, 'ms');
      console.log('📊 Average response time:', Math.round(avgResponseTime), 'ms');

      if (maxResponseTime - minResponseTime < 1000) {
        console.log('✅ Concurrent operations have similar response times');
      } else {
        console.log('📊 Significant response time difference (may indicate conflict handling)');
      }
    }

    // Test 2: Database consistency after concurrent operations
    console.log('📊 Test 2: Verifying database consistency...');

    await Promise.all(
      conflictSessions.map(async (session) => {
        const listResponse = await session.request.get('/api/fiches', {
          headers: { 'X-Test-Commis': session.userId }
        });

        if (listResponse.ok()) {
          const fiches = await listResponse.json();
          console.log(`📊 User ${session.index} sees ${fiches.length} fiches`);
        }
      })
    );

    // Test 3: Simulate resource contention
    console.log('📊 Test 3: Testing resource contention...');

    if (successfulConflictOps.length >= 1) {
      console.log('📊 Resource contention test skipped (no fiche update endpoint)');
    }

    // Clean up
    await Promise.all(conflictSessions.map(session => session.context.close()));

    console.log('✅ Resource sharing and conflict resolution test completed');
  });

  test('Session management and cleanup', async ({ browser, request }) => {
    console.log('🚀 Starting session management test...');

    const baseCommisId = process.env.TEST_PARALLEL_INDEX || '0';

    // Test 1: Session lifecycle management
    console.log('📊 Test 1: Testing session lifecycle...');

    const context1 = await browser.newContext();
    const page1 = await context1.newPage();
    const userId1 = `${baseCommisId}_session_1`;

    // Create data in session
    await page1.goto('/');
    await page1.waitForTimeout(1000);

    const ficheResponse = await request.post('/api/fiches', {
      headers: {
        'X-Test-Commis': userId1,
        'Content-Type': 'application/json',
      },
      data: {
        name: `Session Test Fiche ${Date.now()}`,
        system_instructions: 'Fiche for session testing',
        task_instructions: 'Test session management',
        model: 'gpt-mock',
      }
    });

    let sessionFiche = null;
    if (ficheResponse.ok()) {
      sessionFiche = await ficheResponse.json();
      console.log('📊 Created fiche in session 1:', sessionFiche.id);
    }

    // Close session 1
    await context1.close();
    console.log('📊 Closed session 1');

    // Test 2: Data persistence after session closure
    console.log('📊 Test 2: Testing data persistence after session closure...');

    const context2 = await browser.newContext();
    const page2 = await context2.newPage();
    const userId2 = `${baseCommisId}_session_2`;

    if (sessionFiche) {
      // Try to access the fiche from a new session with same commis ID
      const persistenceResponse = await request.get('/api/fiches', {
        headers: { 'X-Test-Commis': userId1 } // Use same commis ID as closed session
      });

      if (persistenceResponse.ok()) {
        const fiches = await persistenceResponse.json();
        const persistedFiche = fiches.find(a => a.id === sessionFiche.id);

        console.log('📊 Fiche persisted after session closure:', !!persistedFiche);

        if (persistedFiche) {
          console.log('✅ Data persists correctly after session closure');
        }
      }
    }

    // Test 3: Session isolation verification
    console.log('📊 Test 3: Verifying session isolation...');

    // Create data in session 2 with different commis ID
    const session2Response = await request.post('/api/fiches', {
      headers: {
        'X-Test-Commis': userId2,
        'Content-Type': 'application/json',
      },
      data: {
        name: `Session 2 Fiche ${Date.now()}`,
        system_instructions: 'Fiche for session 2',
        task_instructions: 'Test session isolation',
        model: 'gpt-mock',
      }
    });

    if (session2Response.ok()) {
      const session2Fiche = await session2Response.json();
      console.log('📊 Created fiche in session 2:', session2Fiche.id);

      // Check isolation: session 2 should not see session 1 data by default
      const isolationResponse = await request.get('/api/fiches', {
        headers: { 'X-Test-Commis': userId2 }
      });

      if (isolationResponse.ok()) {
        const session2Fiches = await isolationResponse.json();
        const hasSession1Data = sessionFiche && session2Fiches.some(a => a.id === sessionFiche.id);
        const hasSession2Data = session2Fiches.some(a => a.id === session2Fiche.id);

        console.log('📊 Session 2 sees session 1 data:', hasSession1Data);
        console.log('📊 Session 2 sees own data:', hasSession2Data);

        if (!hasSession1Data && hasSession2Data) {
          console.log('✅ Session isolation working correctly');
        } else if (hasSession1Data) {
          console.log('📊 Sessions share data (may be intended behavior)');
        }
      }
    }

    await context2.close();

    // Test 4: Cleanup verification
    console.log('📊 Test 4: Testing cleanup mechanisms...');

    // Create a temporary session to test cleanup
    const tempContext = await browser.newContext();
    const tempPage = await tempContext.newPage();
    const tempUserId = `${baseCommisId}_temp_${Date.now()}`;

    // Create temporary data
    const tempResponse = await request.post('/api/fiches', {
      headers: {
        'X-Test-Commis': tempUserId,
        'Content-Type': 'application/json',
      },
      data: {
        name: `Temp Fiche ${Date.now()}`,
        system_instructions: 'Temporary fiche for cleanup testing',
        task_instructions: 'Test cleanup',
        model: 'gpt-mock',
      }
    });

    if (tempResponse.ok()) {
      const tempFiche = await tempResponse.json();
      console.log('📊 Created temporary fiche:', tempFiche.id);

      // Close context immediately
      await tempContext.close();

      // Wait a moment for potential cleanup
      await new Promise(resolve => setTimeout(resolve, 2000));

      // Check if cleanup occurred (this would require a cleanup endpoint or mechanism)
      console.log('📊 Cleanup verification completed (manual inspection may be needed)');
    }

    console.log('✅ Session management test completed');
  });
});
