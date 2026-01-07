import { test, expect } from './fixtures';

// Ensure every test in this file starts with an empty DB so row counts are
// deterministic across parallel pages.
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database');
});

test('Dashboard live update placeholder', async ({ browser }) => {
  // Open two tabs to simulate multi-tab sync
  const context = await browser.newContext();
  const page1 = await context.newPage();
  await page1.goto('/');
  const page2 = await context.newPage();
  await page2.goto('/');

  // Trigger create in page1
  await page1.locator('[data-testid="create-agent-btn"]').click();

  // Expect row appears in page2 after some time
  await expect(page2.locator('tr[data-agent-id]')).toHaveCount(1, { timeout: 15_000 });
});

test('WebSocket connection establishes successfully', async ({ page }) => {
  console.log('🎯 Testing: WebSocket connection establishment');

  // Track WebSocket connections
  const wsConnections: string[] = [];
  let wsConnected = false;

  page.on('websocket', ws => {
    const url = ws.url();
    wsConnections.push(url);
    wsConnected = true;
    console.log('✅ WebSocket connected:', url);

    // Verify worker parameter is present (from fixtures)
    expect(url).toContain('worker=');
  });

  // Navigate to app
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(2000);

  // Verify at least one WebSocket connection was established
  expect(wsConnected).toBe(true);
  expect(wsConnections.length).toBeGreaterThan(0);
  console.log(`✅ WebSocket connections established: ${wsConnections.length}`);
});

test('Message streaming via WebSocket', async ({ page, request }) => {
  console.log('🎯 Testing: Message streaming through WebSocket');

  // Create agent
  const agentResponse = await request.post('/api/agents', {
    data: {
      name: 'WebSocket Streaming Agent',
      system_instructions: 'Test agent',
      task_instructions: 'Respond briefly',
      model: 'gpt-5-nano',
    }
  });
  expect(agentResponse.status()).toBe(201);
  const agent = await agentResponse.json();
  console.log(`✅ Created agent ID: ${agent.id}`);

  // Track WebSocket messages
  const wsMessages: any[] = [];

  page.on('websocket', ws => {
    console.log('🔌 WebSocket connected');
    ws.on('framereceived', event => {
      try {
        const message = JSON.parse(event.payload);
        wsMessages.push(message);
        if (message.event_type) {
          console.log(`📨 Received: ${message.event_type}`);
        }
      } catch (error) {
        // Ignore non-JSON frames
      }
    });
  });

  // Navigate to chat
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.locator(`[data-testid="chat-agent-${agent.id}"]`).click();
  await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });

  // Send message that will trigger streaming
  await page.getByTestId('chat-input').fill('Say hello');
  await page.getByTestId('send-message-btn').click();

  // Wait for streaming to occur
  await page.waitForTimeout(5000);

  // Verify we received WebSocket messages
  expect(wsMessages.length).toBeGreaterThan(0);
  console.log(`✅ Received ${wsMessages.length} WebSocket messages`);

  // Check for streaming-related events
  const streamEvents = wsMessages.filter(m =>
    m.event_type && (
      m.event_type.includes('stream') ||
      m.event_type === 'stream_start' ||
      m.event_type === 'stream_chunk' ||
      m.event_type === 'stream_end'
    )
  );

  // CRITICAL: Must detect actual streaming events to prove streaming works
  // Logging event types for debugging, but FAILING if no stream events found
  if (streamEvents.length === 0) {
    const eventTypes = wsMessages
      .map(m => m.event_type)
      .filter(Boolean)
      .slice(0, 10);
    console.log(`❌ No streaming events found. Event types received: ${eventTypes.join(', ')}`);
    console.log(`Total WebSocket messages: ${wsMessages.length}`);

    // FAIL the test - streaming must be detected
    expect(streamEvents.length).toBeGreaterThan(0);
    console.log('❌ Test failed: No stream_start/stream_chunk/stream_end events detected');
  } else {
    expect(streamEvents.length).toBeGreaterThan(0);
    console.log(`✅ Detected ${streamEvents.length} streaming events via WebSocket`);
  }
});

test('WebSocket connection recovery after disconnect', async ({ page }) => {
  console.log('🎯 Testing: WebSocket connection recovery');

  let connectionCount = 0;

  page.on('websocket', ws => {
    connectionCount++;
    console.log(`🔌 WebSocket connection #${connectionCount}: ${ws.url()}`);

    ws.on('close', () => {
      console.log(`📡 WebSocket connection #${connectionCount} closed`);
    });
  });

  // Navigate to app (first load)
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(2000);

  // CRITICAL: Capture initial connection count
  const initialConnectionCount = connectionCount;
  expect(initialConnectionCount).toBeGreaterThan(0);
  console.log(`✅ Initial WebSocket connections: ${initialConnectionCount}`);

  // Simulate disconnect via page navigation
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(2000);

  // CRITICAL: Verify NEW connections were created (reconnection occurred)
  const finalConnectionCount = connectionCount;
  expect(finalConnectionCount).toBeGreaterThan(initialConnectionCount);
  console.log(`✅ WebSocket reconnection detected: ${initialConnectionCount} → ${finalConnectionCount}`);
  console.log('✅ Connection recovery validated');
});
