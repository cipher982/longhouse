import { test, expect } from './fixtures';
import { resetDatabase } from './test-utils';
import { createFicheViaUI } from './helpers/fiche-helpers';
import { waitForPageReady } from './helpers/ready-signals';

// Ensure every test in this file starts with an empty DB so row counts are
// deterministic across parallel pages.
// Uses strict reset that throws on failure to fail fast
test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
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

    // Verify commis parameter is present (from fixtures)
    expect(url).toContain('commis=');
  });

  const wsPromise = page.waitForEvent('websocket', { timeout: 10000 });

  // Navigate to app
  await page.goto('/');
  await waitForPageReady(page);

  await wsPromise;

  // Verify at least one WebSocket connection was established
  await expect
    .poll(() => wsConnections.length, { timeout: 10000 })
    .toBeGreaterThan(0);
  expect(wsConnected).toBe(true);
  console.log(`✅ WebSocket connections established: ${wsConnections.length}`);
});

test('Message streaming via WebSocket', async ({ page }) => {
  console.log('🎯 Testing: Message streaming through WebSocket');

  const ficheId = await createFicheViaUI(page);
  console.log(`✅ Created fiche ID: ${ficheId}`);

  // Track WebSocket messages
  const wsMessages: any[] = [];

  page.on('websocket', ws => {
    console.log('🔌 WebSocket connected');
    ws.on('framereceived', event => {
      try {
        const message = JSON.parse(event.payload);
        wsMessages.push(message);
        const messageType = message.type || message.event_type;
        if (messageType) {
          console.log(`📨 Received: ${messageType}`);
        }
      } catch (error) {
        // Ignore non-JSON frames
      }
    });
  });

  // Navigate to chat
  await page.locator(`[data-testid="chat-fiche-${ficheId}"]`).click();
  await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 10000 });

  // Send message that will trigger streaming
  await page.getByTestId('chat-input').fill('Say hello');
  await page.getByTestId('send-message-btn').click();

  // Wait for streaming to occur
  await expect
    .poll(
      () =>
        wsMessages.some((m: any) =>
          ['stream_start', 'stream_chunk', 'stream_end'].includes(m.type)
        ),
      { timeout: 15000 }
    )
    .toBe(true);

  // Verify we received WebSocket messages
  expect(wsMessages.length).toBeGreaterThan(0);
  console.log(`✅ Received ${wsMessages.length} WebSocket messages`);

  // Check for streaming-related events
  const streamEvents = wsMessages.filter((m: any) =>
    ['stream_start', 'stream_chunk', 'stream_end'].includes(m.type)
  );

  // CRITICAL: Must detect actual streaming events to prove streaming works
  // Logging event types for debugging, but FAILING if no stream events found
  if (streamEvents.length === 0) {
    const eventTypes = wsMessages
      .map(m => m.type)
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
  await waitForPageReady(page);

  // CRITICAL: Capture initial connection count
  await expect
    .poll(() => connectionCount, { timeout: 10000 })
    .toBeGreaterThan(0);
  const initialConnectionCount = connectionCount;
  console.log(`✅ Initial WebSocket connections: ${initialConnectionCount}`);

  // Simulate disconnect via page navigation
  await page.reload();
  await waitForPageReady(page);

  // CRITICAL: Verify NEW connections were created (reconnection occurred)
  await expect
    .poll(() => connectionCount, { timeout: 10000 })
    .toBeGreaterThan(initialConnectionCount);
  const finalConnectionCount = connectionCount;
  console.log(`✅ WebSocket reconnection detected: ${initialConnectionCount} → ${finalConnectionCount}`);
  console.log('✅ Connection recovery validated');
});
