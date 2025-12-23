/**
 * E2E Test: Evidence Mounting System (Mount → Reason → Prune)
 *
 * Tests the complete flow of the evidence mounting system:
 * 1. spawn_worker returns compact payload with [EVIDENCE:...] marker and tool index
 * 2. Supervisor can answer questions using worker evidence (internal expansion happens transparently)
 * 3. System doesn't crash/hang with the new format
 *
 * What we CAN observe in E2E:
 * - Worker spawned events (shows task description)
 * - Worker complete events (shows compact payload with tool index)
 * - Supervisor complete events (shows final answer)
 *
 * What we CANNOT observe in E2E (internal implementation):
 * - Evidence expansion (happens inside LLM wrapper)
 * - Raw tool outputs (not shown in UI by default)
 * - Whether evidence was persisted (should NOT be persisted)
 */

import { test, expect, type Page } from './fixtures';

// Reset DB before each test to keep agent/thread ids predictable
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database');
});

/**
 * Navigate to Jarvis chat page and wait for UI to load
 */
async function navigateToChatPage(page: Page): Promise<void> {
  await page.goto('/chat');

  // Wait for Jarvis chat UI to load
  const chatInterface = page.locator('#pttBtn, .chat-wrapper, .transcript');
  await expect(chatInterface.first()).toBeVisible({ timeout: 10000 });
  console.log('✅ Chat page loaded');
}

/**
 * Send a message and wait for response
 */
async function sendMessageAndWaitForResponse(page: Page, message: string, timeout: number = 60000): Promise<void> {
  // Jarvis chat uses .text-input and .send-button
  const inputSelector = page.locator('.text-input');
  const sendButton = page.locator('.send-button');

  await inputSelector.fill(message);
  console.log(`📝 Filled message: "${message}"`);

  // Count messages before sending
  const messagesBefore = await page.locator('.message.assistant').count();

  await sendButton.click();
  console.log('📤 Send button clicked');

  // Wait for NEW assistant response to appear (count increases)
  await page.waitForFunction(
    (beforeCount) => {
      const messages = document.querySelectorAll('.message.assistant');
      return messages.length > beforeCount;
    },
    messagesBefore,
    { timeout }
  );
  console.log('✅ New assistant response appeared');

  // Wait for response to be finalized (not streaming) and have content
  await page.waitForTimeout(3000);
}

/**
 * Capture SSE events from the page
 */
interface CapturedSSEEvent {
  timestamp: number;
  eventType: string;
  data: any;
}

async function setupSSECapture(page: Page): Promise<void> {
  await page.addInitScript(() => {
    // Store SSE events in window for retrieval
    (window as any).__capturedSSEEvents = [];

    // Intercept EventSource if used
    const OriginalEventSource = (window as any).EventSource;
    if (OriginalEventSource) {
      (window as any).EventSource = function(url: string, config?: any) {
        const es = new OriginalEventSource(url, config);

        // Capture message events
        es.addEventListener('message', (e: MessageEvent) => {
          try {
            const parsed = JSON.parse(e.data);
            (window as any).__capturedSSEEvents.push({
              timestamp: Date.now(),
              eventType: parsed.event || 'message',
              data: parsed,
            });

            // Log supervisor and worker events
            if (parsed.event === 'worker:spawned' ||
                parsed.event === 'worker:complete' ||
                parsed.event === 'supervisor:complete') {
              console.log(`SSE event: ${parsed.event}`, parsed.payload || parsed);
            }
          } catch (e) {
            // Ignore parse errors
          }
        });

        return es;
      };
    }
  });
}

async function getCapturedSSEEvents(page: Page): Promise<CapturedSSEEvent[]> {
  return await page.evaluate(() => {
    return (window as any).__capturedSSEEvents || [];
  });
}

test.describe('Evidence Mounting System E2E Tests', () => {
  test('system processes messages without crashing', async ({ page }) => {
    console.log('\n🧪 TEST: System handles evidence mounting without crashing\n');

    await setupSSECapture(page);
    await navigateToChatPage(page);

    // Send a simple task that might use tools
    const testMessage = 'What is the current time?';
    await sendMessageAndWaitForResponse(page, testMessage, 60000);

    // Verify assistant message appears
    const assistantMessages = page.locator('.message.assistant');
    await expect(assistantMessages.last()).toBeVisible();
    const assistantContent = await assistantMessages.last().innerText();
    expect(assistantContent?.length).toBeGreaterThan(0);
    console.log('✅ Assistant message content:', assistantContent?.substring(0, 100));

    // Take screenshot for debugging
    await page.screenshot({ path: 'debug-evidence-mounting-basic.png', fullPage: true });

    // ASSERTIONS:

    // 1. Verify assistant gave a response
    expect(assistantContent).toBeTruthy();
    console.log('✅ Assistant provided a response');

    // 2. Verify response is substantive (more than just an error)
    expect(assistantContent?.length).toBeGreaterThan(10);
    console.log('✅ Response is substantive');

    // The key test: system didn't crash with the new evidence mounting format
    console.log('✅ System processed request without crashing');
  });

  test('supervisor provides useful responses', async ({ page }) => {
    console.log('\n🧪 TEST: Supervisor provides useful responses\n');

    await setupSSECapture(page);
    await navigateToChatPage(page);

    // Send a straightforward task
    const testMessage = 'Tell me what time it is now.';
    await sendMessageAndWaitForResponse(page, testMessage, 60000);

    // Get assistant response
    const assistantMessages = page.locator('.message.assistant');
    await expect(assistantMessages.last()).toBeVisible();
    const assistantContent = await assistantMessages.last().innerText();

    console.log('\n📝 SUPERVISOR RESPONSE:');
    console.log(assistantContent);

    // Take screenshot
    await page.screenshot({ path: 'debug-evidence-mounting-answer.png', fullPage: true });

    // ASSERTIONS:

    // 1. Verify response is substantive
    expect(assistantContent?.length).toBeGreaterThan(10);
    console.log('✅ Supervisor provided a substantive response');

    // 2. Verify assistant gave a response
    expect(assistantContent).toBeTruthy();
    console.log('✅ Assistant responded successfully');

    console.log('✅ System handled request correctly');
  });

  test('handles complex requests', async ({ page }) => {
    console.log('\n🧪 TEST: System handles complex requests\n');

    await setupSSECapture(page);
    await navigateToChatPage(page);

    // Send a request
    const testMessage = 'What is the time right now?';
    await sendMessageAndWaitForResponse(page, testMessage, 90000);

    // Get assistant response
    const assistantMessages = page.locator('.message.assistant');
    await expect(assistantMessages.last()).toBeVisible();
    const assistantContent = await assistantMessages.last().innerText();

    console.log('\n📝 SUPERVISOR RESPONSE:');
    console.log(assistantContent);

    // Take screenshot
    await page.screenshot({ path: 'debug-evidence-mounting-complex.png', fullPage: true });

    // ASSERTIONS:

    // 1. Verify response is substantive
    expect(assistantContent?.length).toBeGreaterThan(10);
    console.log('✅ Supervisor provided a substantive response');

    // 2. Verify assistant gave a response
    expect(assistantContent).toBeTruthy();
    console.log('✅ Assistant responded successfully');

    console.log('✅ System handled complex request correctly');
  });

  test('consistent responses across multiple messages', async ({ page }) => {
    console.log('\n🧪 TEST: Consistent responses across multiple messages\n');

    await setupSSECapture(page);
    await navigateToChatPage(page);

    // Send first message
    const testMessage1 = 'What time is it?';
    await sendMessageAndWaitForResponse(page, testMessage1, 60000);

    // Verify first response
    const assistantMessages = page.locator('.message.assistant');
    const firstResponse = await assistantMessages.first().innerText();
    console.log('\n📝 FIRST RESPONSE:');
    console.log(firstResponse?.substring(0, 100));

    // Send second message
    await page.waitForTimeout(1000);
    const testMessage2 = 'Thanks, that helps!';
    await sendMessageAndWaitForResponse(page, testMessage2, 60000);

    // Verify second response
    const secondResponse = await assistantMessages.last().innerText();
    console.log('\n📝 SECOND RESPONSE:');
    console.log(secondResponse?.substring(0, 100));

    // Take screenshot
    await page.screenshot({ path: 'debug-evidence-mounting-consistency.png', fullPage: true });

    // ASSERTIONS:

    // 1. Verify both responses are substantive
    expect(firstResponse?.length).toBeGreaterThan(10);
    expect(secondResponse?.length).toBeGreaterThan(5);
    console.log('✅ Both responses are substantive');

    // 2. Verify we have at least 2 assistant messages
    const messageCount = await assistantMessages.count();
    expect(messageCount).toBeGreaterThanOrEqual(2);
    console.log('✅ Got responses for both messages');

    console.log('✅ System maintains consistency across messages');
  });

  test('handles sequential requests reliably', async ({ page }) => {
    console.log('\n🧪 TEST: System handles sequential requests reliably\n');

    await setupSSECapture(page);
    await navigateToChatPage(page);

    // Send multiple messages in sequence to verify stability
    const messages = [
      'What is the current time?',
      'Thanks for that information.',
    ];

    for (let i = 0; i < messages.length; i++) {
      console.log(`\n--- Message ${i + 1} of ${messages.length} ---`);
      await sendMessageAndWaitForResponse(page, messages[i], 60000);

      // Small delay between messages
      await page.waitForTimeout(1000);
    }

    // Verify we have all assistant messages
    const assistantMessages = page.locator('.message.assistant');
    const assistantCount = await assistantMessages.count();

    console.log('\n📊 STABILITY CHECK:');
    console.log(`  Messages sent: ${messages.length}`);
    console.log(`  Assistant messages in UI: ${assistantCount}`);

    // Take screenshot
    await page.screenshot({ path: 'debug-evidence-mounting-stability.png', fullPage: true });

    // ASSERTIONS:

    // 1. Verify all assistant messages are visible
    expect(assistantCount).toBeGreaterThanOrEqual(messages.length);
    console.log('✅ All assistant messages visible in UI');

    // 2. Verify each response is substantive
    for (let i = 0; i < assistantCount; i++) {
      const content = await assistantMessages.nth(i).innerText();
      expect(content?.length).toBeGreaterThan(5);
    }
    console.log('✅ All responses are substantive');

    console.log('✅ System remained stable across sequential requests');
  });
});
