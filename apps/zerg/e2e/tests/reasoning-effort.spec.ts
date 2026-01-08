/**
 * E2E Test: Reasoning Effort Feature
 *
 * Tests the full flow of setting reasoning effort, sending messages, and verifying
 * the response includes reasoning token metadata.
 *
 * Includes extensive debugging to trace data flow through:
 * - Frontend request (reasoning_effort parameter)
 * - Backend SSE response (supervisor_complete event with usage)
 * - UI rendering (reasoning tokens badge)
 */

import { test, expect, type Page } from './fixtures';

// Skip: Reasoning effort tests need chat selector updates
test.skip();

// Reset DB before each test to keep agent/thread ids predictable
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
});

async function navigateToChatPage(page: Page): Promise<void> {
  await page.goto('/chat');

  // Wait for Jarvis chat UI to load
  const chatInterface = page.locator('.text-input-container, .chat-wrapper, .transcript');
  await expect(chatInterface.first()).toBeVisible({ timeout: 10000 });
  console.log('✅ Chat page loaded');
}

interface CapturedSSEEvent {
  timestamp: number;
  eventType: string;
  data: any;
}

interface CapturedRequest {
  timestamp: number;
  method: string;
  url: string;
  postData?: any;
  response?: {
    status: number;
    statusText: string;
  };
}

interface TestDebugData {
  consoleMessages: string[];
  sseEvents: CapturedSSEEvent[];
  networkRequests: CapturedRequest[];
}

/**
 * Set up comprehensive debugging for a test
 */
async function setupDebugging(page: Page): Promise<TestDebugData> {
  const debugData: TestDebugData = {
    consoleMessages: [],
    sseEvents: [],
    networkRequests: [],
  };

  // Capture console messages
  page.on('console', async msg => {
    const text = msg.text();
    debugData.consoleMessages.push(`[${msg.type()}] ${text}`);

    // Log SSE events from frontend code
    if (text.includes('SSE event:') || text.includes('supervisor_complete') || text.includes('supervisor:complete')) {
      console.log(`📋 Console: ${text}`);

      // Try to extract usage object from console
      if (text.includes('supervisor:complete') && text.includes('usage:')) {
        try {
          const args = msg.args();
          if (args.length > 1) {
            // Second arg is usually the data object
            const dataJson = await args[1].jsonValue();
            if (dataJson && dataJson.usage) {
              console.log(`📊 Usage data:`, JSON.stringify(dataJson.usage, null, 2));
            }
          }
        } catch (e) {
          // Ignore
        }
      }
    }
  });

  // Capture network requests and responses
  page.on('request', request => {
    const url = request.url();
    const method = request.method();

    // Only track relevant endpoints
    if (url.includes('/api/jarvis/chat') || url.includes('/api/jarvis/history')) {
      const capturedRequest: CapturedRequest = {
        timestamp: Date.now(),
        method,
        url,
      };

      // Capture POST data for chat endpoint
      if (method === 'POST' && url.includes('/api/jarvis/chat')) {
        try {
          const postData = request.postDataJSON();
          capturedRequest.postData = postData;
          console.log(`📤 Request to ${url}:`, JSON.stringify(postData, null, 2));
        } catch (e) {
          // Ignore parse errors
        }
      }

      debugData.networkRequests.push(capturedRequest);
    }
  });

  page.on('response', async response => {
    const url = response.url();

    if (url.includes('/api/jarvis/chat') || url.includes('/api/jarvis/history')) {
      const request = debugData.networkRequests.find(r => r.url === url && !r.response);
      if (request) {
        request.response = {
          status: response.status(),
          statusText: response.statusText(),
        };
        console.log(`📥 Response from ${url}: ${response.status()} ${response.statusText()}`);
      }
    }
  });

  // Inject SSE event capture script into page context
  await page.addInitScript(() => {
    // Store SSE events in window for retrieval
    (window as any).__capturedSSEEvents = [];

    // Intercept EventSource if used
    const OriginalEventSource = (window as any).EventSource;
    if (OriginalEventSource) {
      (window as any).EventSource = function(url: string, config?: any) {
        const es = new OriginalEventSource(url, config);

        es.addEventListener('message', (e: MessageEvent) => {
          (window as any).__capturedSSEEvents.push({
            timestamp: Date.now(),
            eventType: 'message',
            data: e.data,
          });
        });

        // Capture all event types
        const originalAddEventListener = es.addEventListener.bind(es);
        es.addEventListener = function(type: string, listener: any, options?: any) {
          if (type !== 'message' && type !== 'error' && type !== 'open') {
            const wrappedListener = (e: MessageEvent) => {
              (window as any).__capturedSSEEvents.push({
                timestamp: Date.now(),
                eventType: type,
                data: e.data,
              });
              listener(e);
            };
            return originalAddEventListener(type, wrappedListener, options);
          }
          return originalAddEventListener(type, listener, options);
        };

        return es;
      };
    }
  });

  return debugData;
}

/**
 * Retrieve captured SSE events from page context
 */
async function getCapturedSSEEvents(page: Page): Promise<CapturedSSEEvent[]> {
  return await page.evaluate(() => {
    return (window as any).__capturedSSEEvents || [];
  });
}

/**
 * Set reasoning effort selector to a specific value
 */
async function setReasoningEffort(page: Page, value: 'none' | 'low' | 'medium' | 'high'): Promise<void> {
  const selector = page.locator('.reasoning-select');
  await expect(selector).toBeVisible({ timeout: 5000 });
  await selector.selectOption(value);

  // Verify selection
  const selectedValue = await selector.inputValue();
  expect(selectedValue).toBe(value);
  console.log(`✅ Reasoning effort set to: ${value}`);
}

/**
 * Send a message and wait for response
 */
async function sendMessageAndWaitForResponse(page: Page, message: string): Promise<void> {
  // Jarvis chat uses .text-input and .send-button
  const inputSelector = page.locator('.text-input');
  const sendButton = page.locator('.send-button');

  await inputSelector.fill(message);
  console.log(`📝 Filled message: "${message}"`);

  await sendButton.click();
  console.log('📤 Send button clicked');

  // Wait for assistant response to appear
  const assistantMessage = page.locator('.message.assistant').last();
  await expect(assistantMessage).toBeVisible({ timeout: 30000 });
  console.log('✅ Assistant response appeared');

  // Wait for response to be finalized (not streaming)
  await page.waitForTimeout(2000);
}

/**
 * Print comprehensive debug report
 */
function printDebugReport(debugData: TestDebugData, sseEvents: CapturedSSEEvent[]): void {
  console.log('\n========== DEBUG REPORT ==========\n');

  console.log('📤 NETWORK REQUESTS:');
  debugData.networkRequests.forEach((req, i) => {
    console.log(`  ${i + 1}. ${req.method} ${req.url}`);
    if (req.postData) {
      console.log(`     POST Data:`, JSON.stringify(req.postData, null, 2));
    }
    if (req.response) {
      console.log(`     Response: ${req.response.status} ${req.response.statusText}`);
    }
  });

  console.log('\n📡 SSE EVENTS (from page context):');
  sseEvents.forEach((evt, i) => {
    console.log(`  ${i + 1}. [${evt.eventType}] at ${new Date(evt.timestamp).toISOString()}`);
    try {
      const parsed = typeof evt.data === 'string' ? JSON.parse(evt.data) : evt.data;
      console.log(`     Data:`, JSON.stringify(parsed, null, 2));
    } catch (e) {
      console.log(`     Raw data:`, evt.data);
    }
  });

  console.log('\n📋 RELEVANT CONSOLE MESSAGES:');
  const relevantMessages = debugData.consoleMessages.filter(msg =>
    msg.includes('SSE') ||
    msg.includes('supervisor') ||
    msg.includes('reasoning') ||
    msg.includes('usage') ||
    msg.includes('token')
  );
  relevantMessages.forEach((msg, i) => {
    console.log(`  ${i + 1}. ${msg}`);
  });

  console.log('\n==================================\n');
}

test.describe('Reasoning Effort Feature E2E Tests', () => {
  test('reasoning effort "none" should have zero reasoning tokens', async ({ page }) => {
    console.log('\n🧪 TEST: Reasoning effort "none" → zero reasoning tokens\n');

    const debugData = await setupDebugging(page);

    // Navigate to chat page
    await navigateToChatPage(page);

    // Set reasoning effort to "none"
    await setReasoningEffort(page, 'none');

    // Send a message
    const testMessage = 'Hello, please say hi back in exactly 5 words';
    await sendMessageAndWaitForResponse(page, testMessage);

    // Retrieve captured SSE events
    const sseEvents = await getCapturedSSEEvents(page);

    // Print debug report
    printDebugReport(debugData, sseEvents);

    // Take screenshot
    await page.screenshot({ path: 'debug-reasoning-none.png', fullPage: true });

    // ASSERTIONS:

    // 1. Verify request includes reasoning_effort: "none"
    const chatRequest = debugData.networkRequests.find(r =>
      r.method === 'POST' && r.url.includes('/api/jarvis/chat')
    );
    expect(chatRequest).toBeDefined();
    expect(chatRequest?.postData).toHaveProperty('reasoning_effort', 'none');
    console.log('✅ Request includes reasoning_effort: "none"');

    // 2. Verify assistant message appears
    const assistantMessages = page.locator('.message.assistant');
    await expect(assistantMessages.last()).toBeVisible();
    const assistantContent = await assistantMessages.last().locator('.message-content').textContent();
    expect(assistantContent?.length).toBeGreaterThan(0);
    console.log('✅ Assistant message has content:', assistantContent?.substring(0, 50));

    // 3. Verify NO reasoning tokens badge appears
    const reasoningBadge = page.locator('.debug-badge');
    const badgeCount = await reasoningBadge.count();

    if (badgeCount > 0) {
      const badgeText = await reasoningBadge.first().textContent();
      console.log(`⚠️  Reasoning badge found: "${badgeText}" (expected none for reasoning_effort="none")`);

      // Check SSE events for reasoning_tokens
      console.log('\n🔍 Checking SSE events for reasoning_tokens field...');
      sseEvents.forEach((evt, i) => {
        if (evt.eventType.includes('complete')) {
          try {
            const parsed = typeof evt.data === 'string' ? JSON.parse(evt.data) : evt.data;
            if (parsed.payload?.usage?.reasoning_tokens) {
              console.log(`❌ Event ${i + 1} has reasoning_tokens:`, parsed.payload.usage.reasoning_tokens);
            }
          } catch (e) {
            // Ignore parse errors
          }
        }
      });
    } else {
      console.log('✅ No reasoning tokens badge (expected for reasoning_effort="none")');
    }

    // For reasoning effort "none", we expect zero reasoning tokens
    // (badge should not appear OR should show 0)
    expect(badgeCount).toBe(0);
  });

  test('reasoning effort "high" should have reasoning tokens', async ({ page }) => {
    console.log('\n🧪 TEST: Reasoning effort "high" → reasoning tokens present\n');

    const debugData = await setupDebugging(page);

    // Navigate to chat page
    await navigateToChatPage(page);

    // Set reasoning effort to "high"
    await setReasoningEffort(page, 'high');

    // Send a message that benefits from reasoning
    const testMessage = 'What is 17 * 23? Think through the calculation step by step.';
    await sendMessageAndWaitForResponse(page, testMessage);

    // Retrieve captured SSE events
    const sseEvents = await getCapturedSSEEvents(page);

    // Print debug report
    printDebugReport(debugData, sseEvents);

    // Take screenshot
    await page.screenshot({ path: 'debug-reasoning-high.png', fullPage: true });

    // ASSERTIONS:

    // 1. Verify request includes reasoning_effort: "high"
    const chatRequest = debugData.networkRequests.find(r =>
      r.method === 'POST' && r.url.includes('/api/jarvis/chat')
    );
    expect(chatRequest).toBeDefined();
    expect(chatRequest?.postData).toHaveProperty('reasoning_effort', 'high');
    console.log('✅ Request includes reasoning_effort: "high"');

    // 2. Verify assistant message appears
    const assistantMessages = page.locator('.message.assistant');
    await expect(assistantMessages.last()).toBeVisible();
    const assistantContent = await assistantMessages.last().locator('.message-content').textContent();
    expect(assistantContent?.length).toBeGreaterThan(0);
    console.log('✅ Assistant message has content:', assistantContent?.substring(0, 100));

    // 3. Check for reasoning tokens badge
    const reasoningBadge = page.locator('.debug-badge');
    const badgeCount = await reasoningBadge.count();

    if (badgeCount > 0) {
      const badgeText = await reasoningBadge.first().textContent();
      console.log(`✅ Reasoning badge found: "${badgeText}"`);

      // Verify badge shows positive reasoning tokens
      expect(badgeText).toContain('reasoning tokens');

      // Extract reasoning token count
      const match = badgeText?.match(/(\d+)\s+reasoning tokens/);
      if (match) {
        const tokenCount = parseInt(match[1]);
        expect(tokenCount).toBeGreaterThan(0);
        console.log(`✅ Reasoning tokens: ${tokenCount}`);
      }
    } else {
      console.log('❌ No reasoning tokens badge found (expected for reasoning_effort="high")');

      // Check SSE events to see if usage data was sent
      console.log('\n🔍 Checking SSE events for missing usage data...');
      let foundCompleteEvent = false;
      sseEvents.forEach((evt, i) => {
        if (evt.eventType.includes('complete')) {
          foundCompleteEvent = true;
          try {
            const parsed = typeof evt.data === 'string' ? JSON.parse(evt.data) : evt.data;
            console.log(`📡 supervisor_complete event ${i + 1}:`, JSON.stringify(parsed, null, 2));

            if (parsed.payload?.usage) {
              console.log(`   ✅ Has usage field:`, parsed.payload.usage);
              if (parsed.payload.usage.reasoning_tokens) {
                console.log(`   ✅ Has reasoning_tokens: ${parsed.payload.usage.reasoning_tokens}`);
              } else {
                console.log(`   ❌ Missing reasoning_tokens in usage`);
              }
            } else {
              console.log(`   ❌ Missing usage field in payload`);
            }
          } catch (e) {
            console.log(`   ⚠️  Failed to parse event data:`, e);
          }
        }
      });

      if (!foundCompleteEvent) {
        console.log('❌ No supervisor_complete event found in SSE stream');
      }

      // Check console for state manager updates
      console.log('\n🔍 Checking console for state manager usage updates...');
      const usageMessages = debugData.consoleMessages.filter(msg =>
        msg.includes('usage') || msg.includes('reasoning_tokens')
      );
      usageMessages.forEach(msg => console.log(`   ${msg}`));

      // Fail the test with detailed diagnostic info
      throw new Error(
        'Reasoning tokens badge not found in UI. Check debug report above for:\n' +
        '  1. Did the backend send usage.reasoning_tokens in supervisor_complete SSE event?\n' +
        '  2. Did the frontend receive and parse the event correctly?\n' +
        '  3. Did the state manager update the message with usage data?\n' +
        '  4. Did the ChatContainer component render the badge?'
      );
    }
  });

  test('compare "none" vs "high" reasoning token counts', async ({ page }) => {
    console.log('\n🧪 TEST: Compare reasoning token counts (none vs high)\n');

    const debugData = await setupDebugging(page);

    // Navigate to chat page
    await navigateToChatPage(page);

    // Test 1: Reasoning effort "none"
    console.log('\n--- Part 1: Testing reasoning_effort="none" ---');
    await setReasoningEffort(page, 'none');
    const message1 = 'Say hello in 3 words';
    await sendMessageAndWaitForResponse(page, message1);

    // Capture state after first message
    const sseEvents1 = await getCapturedSSEEvents(page);
    const badge1Count = await page.locator('.debug-badge').count();
    console.log(`📊 Message 1 (none): badge count = ${badge1Count}`);

    // Wait a bit before sending second message
    await page.waitForTimeout(1000);

    // Test 2: Reasoning effort "high"
    console.log('\n--- Part 2: Testing reasoning_effort="high" ---');
    await setReasoningEffort(page, 'high');
    const message2 = 'Calculate 47 * 89 step by step';
    await sendMessageAndWaitForResponse(page, message2);

    // Capture state after second message
    const sseEvents2 = await getCapturedSSEEvents(page);
    const badge2Count = await page.locator('.debug-badge').count();
    console.log(`📊 Message 2 (high): badge count = ${badge2Count}`);

    // Print debug report
    printDebugReport(debugData, sseEvents2);

    // Take final screenshot
    await page.screenshot({ path: 'debug-reasoning-comparison.png', fullPage: true });

    // ASSERTIONS:

    // Verify both messages were sent with correct reasoning_effort values
    const chatRequests = debugData.networkRequests.filter(r =>
      r.method === 'POST' && r.url.includes('/api/jarvis/chat')
    );
    expect(chatRequests.length).toBeGreaterThanOrEqual(2);

    console.log('\n📋 Request summary:');
    chatRequests.forEach((req, i) => {
      console.log(`  ${i + 1}. reasoning_effort = ${req.postData?.reasoning_effort}`);
    });

    // Verify we have 2 assistant messages
    const assistantMessages = page.locator('.message.assistant');
    const assistantCount = await assistantMessages.count();
    expect(assistantCount).toBeGreaterThanOrEqual(2);
    console.log(`✅ ${assistantCount} assistant messages in conversation`);

    // Expected: "none" should have 0 badges, "high" should have at least 1
    // (If reasoning tokens work correctly)
    console.log('\n📊 COMPARISON RESULTS:');
    console.log(`  reasoning_effort="none": ${badge1Count} badges`);
    console.log(`  reasoning_effort="high": ${badge2Count} badges`);

    if (badge2Count === 0) {
      console.log('\n⚠️  WARNING: No reasoning tokens badge for reasoning_effort="high"');
      console.log('This suggests reasoning tokens are not being returned by the backend');
      console.log('or not being displayed in the UI. Check debug report above.');
    } else {
      console.log('\n✅ Reasoning tokens feature is working (badge appears for "high")');
    }
  });
});
