/**
 * Core User Journey E2E Test
 *
 * Tests the concierge -> response flow using the gpt-scripted model
 * for deterministic behavior without real LLM calls.
 *
 * The gpt-scripted model has predefined scenarios that emit specific responses
 * based on message patterns. This enables fully deterministic E2E testing.
 *
 * Primary test: generic_concierge scenario
 * - User sends any message (e.g., "hello jarvis")
 * - Concierge returns direct scripted response
 * - No commis spawning (avoids continuation complexity)
 *
 * For commis flow testing with spawn_commis, see TODO: commis_flow.spec.ts
 */

import { randomUUID } from 'node:crypto';

import { test, expect, type Page } from '../fixtures';

/**
 * Navigate to Jarvis chat page and wait for it to be ready
 */
async function navigateToChatPage(page: Page): Promise<void> {
  await page.goto('/chat');
  // Wait for chat interface using data-testid (not CSS class per banana handoff)
  await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 15000 });
}

/**
 * Parse SSE text into structured events
 * Handles both \n and \r\n line endings, and captures final event if stream doesn't end with blank line
 */
function parseSSEEvents(sseText: string): Array<{ event: string; data: unknown }> {
  const events: Array<{ event: string; data: unknown }> = [];
  // Normalize line endings to \n
  const lines = sseText.replace(/\r\n/g, '\n').split('\n');
  let currentEvent = '';
  const currentDataLines: string[] = [];

  const pushEvent = () => {
    const currentData = currentDataLines.join('\n').trim();
    if (currentEvent && currentData) {
      try {
        events.push({ event: currentEvent, data: JSON.parse(currentData) });
      } catch {
        events.push({ event: currentEvent, data: currentData });
      }
      currentEvent = '';
      currentDataLines.length = 0;
    }
  };

  for (const line of lines) {
    if (line.startsWith('event:')) {
      currentEvent = line.substring(6).trim();
    } else if (line.startsWith('data:')) {
      currentDataLines.push(line.substring(5));
    } else if (line === '') {
      pushEvent();
    }
  }

  // Capture final event if stream doesn't end with blank line
  pushEvent();

  return events;
}

/**
 * Query course events from the API
 */
async function getCourseEvents(
  request: import('@playwright/test').APIRequestContext,
  courseId: number,
  eventType?: string
): Promise<{ events: Array<{ event_type: string; payload: Record<string, unknown> }>; total: number }> {
  const url = eventType
    ? `/api/jarvis/courses/${courseId}/events?event_type=${eventType}`
    : `/api/jarvis/courses/${courseId}/events`;

  const response = await request.get(url);
  expect(response.status()).toBe(200);
  return response.json();
}

test.describe('Core User Journey - Scripted LLM', () => {
  // Set longer timeout for this test as it involves full concierge flow
  test.setTimeout(120000);

  test('concierge direct response flow with gpt-scripted model', async ({ page, request }) => {
    console.log('[Core Journey] Starting test');

    // Send a simple message that uses the "generic_concierge" scenario
    // This scenario returns a direct response without spawning commis,
    // which avoids the continuation flow complexity for this core test.
    //
    // For commis flow testing, see commis_flow.spec.ts (TODO)
    const chatResponse = await request.post('/api/jarvis/chat', {
      data: {
        message: 'hello jarvis',
        message_id: randomUUID(),
        model: 'gpt-scripted',
        client_correlation_id: 'e2e-core-journey-test',
      },
    });

    expect(chatResponse.status()).toBe(200);
    console.log('[Core Journey] Chat request sent with gpt-scripted model');

    // The response is SSE stream, consume it
    const sseText = await chatResponse.text();
    console.log('[Core Journey] SSE response received, length:', sseText.length);

    // Parse SSE events
    const events = parseSSEEvents(sseText);
    console.log(
      '[Core Journey] Parsed SSE events:',
      events.map((e) => e.event)
    );

    // Step 3: Extract course_id from connected event
    const connectedEvent = events.find((e) => e.event === 'connected');
    expect(connectedEvent).toBeTruthy();
    const courseId = (connectedEvent?.data as { course_id?: number })?.course_id;
    expect(courseId).toBeTruthy();
    console.log('[Core Journey] Course ID:', courseId);

    // Step 4: Verify we got concierge_complete event
    // The generic_concierge scenario doesn't spawn commis, so we get complete directly
    const completeEvent = events.find((e) => e.event === 'concierge_complete');
    expect(completeEvent).toBeTruthy();
    console.log('[Core Journey] Found concierge_complete event');

    // Step 5: Extract and verify response contains expected scripted text
    const completePayload = (completeEvent?.data as { payload?: { result?: string } })?.payload;
    const result = completePayload?.result || '';
    console.log('[Core Journey] Result:', result.substring(0, 200));

    // The generic_fallback scenario returns a deterministic "ok"
    expect(result.toLowerCase()).toBe('ok');
    console.log('[Core Journey] Expected scripted response text found');

    // Step 6: Query events API to verify course execution was recorded
    // Use polling instead of sleep to wait for event persistence (per banana handoff)
    let allEvents = await getCourseEvents(request, courseId!);

    // Poll until trace includes concierge lifecycle evidence (events are persisted async)
    await expect.poll(
      async () => {
        allEvents = await getCourseEvents(request, courseId!);
        const types = allEvents.events.map((e) => e.event_type);
        return {
          total: allEvents.total,
          hasConciergeStarted: types.includes('concierge_started'),
        };
      },
      { timeout: 20000, intervals: [200, 500, 1000, 2000] }
    ).toEqual(expect.objectContaining({ hasConciergeStarted: true }));

    console.log('[Core Journey] Total events for course:', allEvents.total);
    console.log(
      '[Core Journey] Event types:',
      allEvents.events.map((e) => e.event_type)
    );

    // Verify we have evidence of concierge execution
    const hasConciergeStart = allEvents.events.some((e) => e.event_type === 'concierge_started');
    expect(hasConciergeStart).toBeTruthy();
    console.log('[Core Journey] concierge_started event found in trace');

    console.log('[Core Journey] Test completed successfully');
  });

  test('course status indicator is present in DOM', async ({ page }) => {
    console.log('[Status Indicator] Starting test');

    await navigateToChatPage(page);

    // Verify the course status indicator exists in the DOM
    const statusIndicator = page.locator('[data-testid="course-status"]');
    await expect(statusIndicator).toBeAttached({ timeout: 5000 });

    // Verify initial state is idle
    await expect(statusIndicator).toHaveAttribute('data-course-status', 'idle', { timeout: 5000 });
    console.log('[Status Indicator] Initial state is idle');

    console.log('[Status Indicator] Test completed successfully');
  });

  test('commis tool rows display command preview', async ({ page }) => {
    console.log('[Commis Tool UI] Starting test');

    await navigateToChatPage(page);

    // Ensure dev-only event bus is available (Playwright uses Vite dev server).
    await page.waitForFunction(() => (window as any).__jarvis?.eventBus != null, null, { timeout: 15000 });

    const courseId = 101;
    const toolCallId = 'call-spawn-1';
    const commisId = 'e2e-commis-1';
    const jobId = 9001;

    await page.evaluate(
      ({ courseId, toolCallId, commisId, jobId }) => {
        const bus = (window as any).__jarvis.eventBus;
        const now = Date.now();

        bus.emit('concierge:started', { courseId, task: 'Test task', timestamp: now });
        bus.emit('concierge:tool_started', {
          courseId,
          toolName: 'spawn_commis',
          toolCallId,
          argsPreview: 'spawn_commis args',
          args: { task: 'Check disk space on cube' },
          timestamp: now + 1,
        });
        bus.emit('concierge:commis_spawned', {
          jobId,
          task: 'Check disk space on cube',
          toolCallId,
          timestamp: now + 2,
        });
        bus.emit('concierge:commis_started', {
          jobId,
          commisId,
          timestamp: now + 3,
        });
        bus.emit('commis:tool_started', {
          commisId,
          toolName: 'runner_exec',
          toolCallId: 'call-tool-1',
          argsPreview: "{'target':'cube','command':'df -h'}",
          timestamp: now + 4,
        });
        bus.emit('commis:tool_completed', {
          commisId,
          toolName: 'runner_exec',
          toolCallId: 'call-tool-1',
          durationMs: 12,
          resultPreview: "{'exit_code': 0, 'stdout': 'ok'}",
          timestamp: now + 5,
        });
      },
      { courseId, toolCallId, commisId, jobId }
    );

    const commisCard = page.locator('.commis-tool-card').first();
    await expect(commisCard).toBeVisible({ timeout: 2000 });

    const commandLabel = commisCard.locator('.nested-tool-name--command');
    await expect(commandLabel).toContainText('df -h', { timeout: 2000 });

    const toolMeta = commisCard.locator('.nested-tool-meta');
    await expect(toolMeta).toContainText('runner_exec', { timeout: 2000 });
    await expect(toolMeta).toContainText('target: cube', { timeout: 2000 });
    await expect(toolMeta).toContainText('exit 0', { timeout: 2000 });

    console.log('[Commis Tool UI] Command preview verified');
  });

  test('nested tool details drawer expands on click', async ({ page }) => {
    console.log('[Details Drawer] Starting test');

    await navigateToChatPage(page);
    await page.waitForFunction(() => (window as any).__jarvis?.eventBus != null, null, { timeout: 15000 });

    const courseId = 201;
    const toolCallId = 'call-spawn-details';
    const commisId = 'e2e-commis-details';
    const jobId = 9002;

    await page.evaluate(
      ({ courseId, toolCallId, commisId, jobId }) => {
        const bus = (window as any).__jarvis.eventBus;
        const now = Date.now();

        bus.emit('concierge:started', { courseId, task: 'Details test', timestamp: now });
        bus.emit('concierge:tool_started', {
          courseId,
          toolName: 'spawn_commis',
          toolCallId,
          argsPreview: 'spawn_commis args',
          args: { task: 'Test details drawer' },
          timestamp: now + 1,
        });
        bus.emit('concierge:commis_spawned', {
          jobId,
          task: 'Test details drawer',
          toolCallId,
          timestamp: now + 2,
        });
        bus.emit('concierge:commis_started', {
          jobId,
          commisId,
          timestamp: now + 3,
        });
        bus.emit('commis:tool_started', {
          commisId,
          toolName: 'ssh_exec',
          toolCallId: 'call-tool-details',
          argsPreview: '{"target":"cube","command":"ls -la /tmp"}',
          timestamp: now + 4,
        });
        bus.emit('commis:tool_completed', {
          commisId,
          toolName: 'ssh_exec',
          toolCallId: 'call-tool-details',
          durationMs: 25,
          argsPreview: '{"target":"cube","command":"ls -la /tmp"}',
          resultPreview: '{"exit_code": 0, "stdout": "drwxrwxrwt 15 root root 4096 Jan 16 10:00 ."}',
          timestamp: now + 5,
        });
      },
      { courseId, toolCallId, commisId, jobId }
    );

    const commisCard = page.locator('.commis-tool-card').first();
    await expect(commisCard).toBeVisible({ timeout: 2000 });

    // Click on the nested tool row to expand details
    const nestedToolRow = commisCard.locator('.nested-tool-row').first();
    await nestedToolRow.click();

    // Verify details drawer is visible
    const detailsDrawer = commisCard.locator('[data-testid="nested-tool-details"]');
    await expect(detailsDrawer).toBeVisible({ timeout: 2000 });

    // Verify content sections are present
    await expect(detailsDrawer.locator('.nested-tool-details__label').first()).toContainText('Args', { timeout: 2000 });
    await expect(detailsDrawer.locator('.nested-tool-details__content').first()).toBeVisible({ timeout: 2000 });

    console.log('[Details Drawer] Details drawer expands correctly');
  });

  test('source badge displays for exec tools', async ({ page }) => {
    console.log('[Source Badge] Starting test');

    await navigateToChatPage(page);
    await page.waitForFunction(() => (window as any).__jarvis?.eventBus != null, null, { timeout: 15000 });

    const courseId = 202;
    const toolCallId = 'call-spawn-source';
    const commisId = 'e2e-commis-source';
    const jobId = 9003;

    await page.evaluate(
      ({ courseId, toolCallId, commisId, jobId }) => {
        const bus = (window as any).__jarvis.eventBus;
        const now = Date.now();

        bus.emit('concierge:started', { courseId, task: 'Source badge test', timestamp: now });
        bus.emit('concierge:tool_started', {
          courseId,
          toolName: 'spawn_commis',
          toolCallId,
          argsPreview: 'spawn_commis args',
          args: { task: 'Test source badge' },
          timestamp: now + 1,
        });
        bus.emit('concierge:commis_spawned', {
          jobId,
          task: 'Test source badge',
          toolCallId,
          timestamp: now + 2,
        });
        bus.emit('concierge:commis_started', {
          jobId,
          commisId,
          timestamp: now + 3,
        });
        bus.emit('commis:tool_started', {
          commisId,
          toolName: 'runner_exec',
          toolCallId: 'call-tool-source',
          argsPreview: '{"target":"laptop","command":"uname -a"}',
          timestamp: now + 4,
        });
      },
      { courseId, toolCallId, commisId, jobId }
    );

    const commisCard = page.locator('.commis-tool-card').first();
    await expect(commisCard).toBeVisible({ timeout: 2000 });

    // Verify source badge is visible
    const sourceBadge = commisCard.locator('.nested-tool-meta-item--source');
    await expect(sourceBadge).toContainText('Runner', { timeout: 2000 });

    console.log('[Source Badge] Source badge displays correctly');
  });

  test('offline badge displays for connection errors', async ({ page }) => {
    console.log('[Offline Badge] Starting test');

    await navigateToChatPage(page);
    await page.waitForFunction(() => (window as any).__jarvis?.eventBus != null, null, { timeout: 15000 });

    const courseId = 203;
    const toolCallId = 'call-spawn-offline';
    const commisId = 'e2e-commis-offline';
    const jobId = 9004;

    await page.evaluate(
      ({ courseId, toolCallId, commisId, jobId }) => {
        const bus = (window as any).__jarvis.eventBus;
        const now = Date.now();

        bus.emit('concierge:started', { courseId, task: 'Offline badge test', timestamp: now });
        bus.emit('concierge:tool_started', {
          courseId,
          toolName: 'spawn_commis',
          toolCallId,
          argsPreview: 'spawn_commis args',
          args: { task: 'Test offline badge' },
          timestamp: now + 1,
        });
        bus.emit('concierge:commis_spawned', {
          jobId,
          task: 'Test offline badge',
          toolCallId,
          timestamp: now + 2,
        });
        bus.emit('concierge:commis_started', {
          jobId,
          commisId,
          timestamp: now + 3,
        });
        bus.emit('commis:tool_started', {
          commisId,
          toolName: 'runner_exec',
          toolCallId: 'call-tool-offline',
          argsPreview: '{"target":"cube","command":"whoami"}',
          timestamp: now + 4,
        });
        bus.emit('commis:tool_failed', {
          commisId,
          toolName: 'runner_exec',
          toolCallId: 'call-tool-offline',
          durationMs: 5000,
          error: 'Runner offline: cube is not responding',
          timestamp: now + 5,
        });
      },
      { courseId, toolCallId, commisId, jobId }
    );

    const commisCard = page.locator('.commis-tool-card').first();
    await expect(commisCard).toBeVisible({ timeout: 2000 });

    // Verify offline badge is visible
    const offlineBadge = commisCard.locator('.nested-tool-meta-item--offline');
    await expect(offlineBadge).toContainText('Runner offline', { timeout: 2000 });

    console.log('[Offline Badge] Offline badge displays correctly');
  });

  test('compact mode toggle hides previews', async ({ page }) => {
    console.log('[Compact Mode] Starting test');

    await navigateToChatPage(page);
    await page.waitForFunction(() => (window as any).__jarvis?.eventBus != null, null, { timeout: 15000 });

    const courseId = 204;
    const toolCallId = 'call-spawn-compact';
    const commisId = 'e2e-commis-compact';
    const jobId = 9005;

    await page.evaluate(
      ({ courseId, toolCallId, commisId, jobId }) => {
        const bus = (window as any).__jarvis.eventBus;
        const now = Date.now();

        bus.emit('concierge:started', { courseId, task: 'Compact mode test', timestamp: now });
        bus.emit('concierge:tool_started', {
          courseId,
          toolName: 'spawn_commis',
          toolCallId,
          argsPreview: 'spawn_commis args',
          args: { task: 'Test compact mode' },
          timestamp: now + 1,
        });
        bus.emit('concierge:commis_spawned', {
          jobId,
          task: 'Test compact mode',
          toolCallId,
          timestamp: now + 2,
        });
        bus.emit('concierge:commis_started', {
          jobId,
          commisId,
          timestamp: now + 3,
        });
        bus.emit('commis:tool_started', {
          commisId,
          toolName: 'ssh_exec',
          toolCallId: 'call-tool-compact',
          argsPreview: '{"target":"cube","command":"echo hello"}',
          timestamp: now + 4,
        });
        bus.emit('commis:tool_completed', {
          commisId,
          toolName: 'ssh_exec',
          toolCallId: 'call-tool-compact',
          durationMs: 10,
          resultPreview: '{"exit_code": 0, "stdout": "hello world output here"}',
          timestamp: now + 5,
        });
      },
      { courseId, toolCallId, commisId, jobId }
    );

    const commisCard = page.locator('.commis-tool-card').first();
    await expect(commisCard).toBeVisible({ timeout: 2000 });

    // Verify preview is visible initially
    const previewText = commisCard.locator('.nested-tool-preview');
    await expect(previewText).toBeVisible({ timeout: 2000 });

    // Click compact toggle
    const compactToggle = commisCard.locator('.commis-tool-card__compact-toggle');
    await compactToggle.click();

    // Verify compact class is applied
    await expect(commisCard).toHaveClass(/commis-tool-card--compact/, { timeout: 2000 });

    // Preview should be hidden in compact mode (CSS display: none)
    await expect(previewText).not.toBeVisible({ timeout: 2000 });

    console.log('[Compact Mode] Compact mode toggle works correctly');
  });

  test('copy button is visible for command tools', async ({ page }) => {
    console.log('[Copy Button] Starting test');

    await navigateToChatPage(page);
    await page.waitForFunction(() => (window as any).__jarvis?.eventBus != null, null, { timeout: 15000 });

    const courseId = 205;
    const toolCallId = 'call-spawn-copy';
    const commisId = 'e2e-commis-copy';
    const jobId = 9006;

    await page.evaluate(
      ({ courseId, toolCallId, commisId, jobId }) => {
        const bus = (window as any).__jarvis.eventBus;
        const now = Date.now();

        bus.emit('concierge:started', { courseId, task: 'Copy button test', timestamp: now });
        bus.emit('concierge:tool_started', {
          courseId,
          toolName: 'spawn_commis',
          toolCallId,
          argsPreview: 'spawn_commis args',
          args: { task: 'Test copy button' },
          timestamp: now + 1,
        });
        bus.emit('concierge:commis_spawned', {
          jobId,
          task: 'Test copy button',
          toolCallId,
          timestamp: now + 2,
        });
        bus.emit('concierge:commis_started', {
          jobId,
          commisId,
          timestamp: now + 3,
        });
        bus.emit('commis:tool_started', {
          commisId,
          toolName: 'runner_exec',
          toolCallId: 'call-tool-copy',
          argsPreview: '{"target":"laptop","command":"pwd"}',
          timestamp: now + 4,
        });
      },
      { courseId, toolCallId, commisId, jobId }
    );

    const commisCard = page.locator('.commis-tool-card').first();
    await expect(commisCard).toBeVisible({ timeout: 2000 });

    // Hover over the nested tool row to make copy button visible
    const nestedToolRow = commisCard.locator('.nested-tool-row').first();
    await nestedToolRow.hover();

    // Verify copy button exists (it's there but hidden until hover in CSS)
    const copyButton = commisCard.locator('.nested-tool-copy');
    await expect(copyButton).toBeAttached({ timeout: 2000 });

    // Clicking copy button should not throw an error
    await copyButton.click();

    console.log('[Copy Button] Copy button is present and clickable');
  });
});

test.describe('Core Journey - API Flow', () => {
  test.setTimeout(60000);

  test('concierge_complete event contains result text', async ({ request }) => {
    // Send a simple message via API
    const chatResponse = await request.post('/api/jarvis/chat', {
      data: {
        message: 'hello',
        message_id: randomUUID(),
        model: 'gpt-mock',
        client_correlation_id: 'e2e-api-test',
      },
    });

    expect(chatResponse.status()).toBe(200);
    const sseText = await chatResponse.text();
    const events = parseSSEEvents(sseText);

    // Verify connected event
    const connectedEvent = events.find((e) => e.event === 'connected');
    expect(connectedEvent).toBeTruthy();

    // Verify concierge_complete event exists
    const completeEvent = events.find((e) => e.event === 'concierge_complete');
    expect(completeEvent).toBeTruthy();

    // Verify the payload structure
    const completePayload = (completeEvent?.data as { payload?: { result?: string; status?: string } })?.payload;
    expect(completePayload).toBeTruthy();
    expect(completePayload?.status).toBe('success');
    console.log('[API Flow] concierge_complete event validated');
  });

  test('course events endpoint returns events for a course', async ({ request }) => {
    // First, create a course
    const chatResponse = await request.post('/api/jarvis/chat', {
      data: {
        message: 'test message',
        message_id: randomUUID(),
        model: 'gpt-mock',
        client_correlation_id: 'e2e-events-test',
      },
    });

    expect(chatResponse.status()).toBe(200);
    const sseText = await chatResponse.text();
    const events = parseSSEEvents(sseText);

    // Extract course_id
    const connectedEvent = events.find((e) => e.event === 'connected');
    const courseId = (connectedEvent?.data as { course_id?: number })?.course_id;
    expect(courseId).toBeTruthy();

    // Query the events endpoint
    const eventsResponse = await request.get(`/api/jarvis/courses/${courseId}/events`);
    expect(eventsResponse.status()).toBe(200);

    const eventsData = await eventsResponse.json();
    expect(eventsData.course_id).toBe(courseId);
    expect(eventsData.events).toBeInstanceOf(Array);
    expect(eventsData.total).toBeGreaterThanOrEqual(0);

    console.log('[Events API] Endpoint returns valid response structure');
  });
});
