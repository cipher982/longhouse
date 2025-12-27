/**
 * E2E Test: Chat Performance Evaluation
 *
 * Tests chat latency across different scenarios:
 * 1. Simple query (no workers) - baseline latency
 * 2. Query with worker spawn - worker latency
 * 3. Query with tool execution - tool latency
 *
 * Captures timeline events, exports metrics to JSON, and asserts on timing thresholds.
 */

import { test, expect, type Page } from './fixtures';
import { TimelineCapture } from '../helpers/timeline-capture';

// Reset DB before each test
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database');
});

/**
 * Navigate to Jarvis chat page and wait for UI to load
 */
async function navigateToChatPage(page: Page): Promise<void> {
  await page.goto('/chat');

  // Wait for chat interface to load
  const chatInterface = page.locator('.text-input-container, .chat-wrapper, .transcript');
  await expect(chatInterface.first()).toBeVisible({ timeout: 10000 });
  console.log('✅ Chat page loaded');
}

/**
 * Send a message and wait for assistant response
 */
async function sendMessage(page: Page, message: string): Promise<void> {
  const inputSelector = page.locator('.text-input');
  const sendButton = page.locator('.send-button');

  await inputSelector.fill(message);
  console.log(`📝 Sending message: "${message}"`);

  await sendButton.click();
  console.log('📤 Message sent');
}

/**
 * Wait for assistant response to complete
 */
async function waitForResponse(page: Page): Promise<void> {
  // Wait for assistant message to appear
  const assistantMessage = page.locator('.message.assistant').last();
  await expect(assistantMessage).toBeVisible({ timeout: 30000 });
  console.log('✅ Assistant response appeared');

  // Wait for streaming to complete (response is finalized)
  await page.waitForTimeout(2000);
}

test.describe('Chat Performance Evaluation', () => {
  test('simple query - baseline latency', async ({ page }) => {
    console.log('\n🧪 TEST: Simple query (no workers) - baseline latency\n');

    await navigateToChatPage(page);

    // Start timeline capture
    const timeline = new TimelineCapture(page);
    await timeline.start();

    // Send simple query
    await sendMessage(page, 'Say hello in exactly 3 words');
    await waitForResponse(page);

    // Stop capture and get events
    const events = await timeline.stop();

    console.log('\n📊 Timeline Summary:');
    console.log(`  Total duration: ${events.totalDurationMs}ms`);
    console.log(`  Correlation ID: ${events.correlationId}`);
    console.log(`  Run ID: ${events.runId}`);
    console.log(`  Event count: ${events.events.length}`);

    // Calculate summary
    const summary = (timeline as any).calculateSummary(events);
    console.log('\n📈 Summary Metrics:');
    console.log(`  Total: ${summary.totalDurationMs}ms`);
    if (summary.supervisorThinkingMs !== undefined) {
      console.log(`  Supervisor thinking: ${summary.supervisorThinkingMs}ms`);
    }
    if (summary.workerExecutionMs !== undefined) {
      console.log(`  Worker execution: ${summary.workerExecutionMs}ms`);
    }
    if (summary.toolExecutionMs !== undefined) {
      console.log(`  Tool execution: ${summary.toolExecutionMs}ms`);
    }

    // Assertions
    expect(events.totalDurationMs).toBeGreaterThan(0);
    expect(events.totalDurationMs).toBeLessThan(30000); // Max 30s for simple query
    expect(events.events.length).toBeGreaterThan(0);
    expect(events.correlationId).not.toBeNull();

    // Export metrics
    const metricsPath = await timeline.exportMetrics('simple-query');
    console.log(`\n💾 Metrics exported to: ${metricsPath}`);
  });

  test('worker spawn query - worker latency', async ({ page }) => {
    console.log('\n🧪 TEST: Query with worker spawn - worker latency\n');

    await navigateToChatPage(page);

    // Start timeline capture
    const timeline = new TimelineCapture(page);
    await timeline.start();

    // Send query that requires worker (uses keyword "check")
    await sendMessage(page, 'Check what time it is right now');
    await waitForResponse(page);

    // Stop capture and get events
    const events = await timeline.stop();

    console.log('\n📊 Timeline Summary:');
    console.log(`  Total duration: ${events.totalDurationMs}ms`);
    console.log(`  Correlation ID: ${events.correlationId}`);
    console.log(`  Run ID: ${events.runId}`);
    console.log(`  Event count: ${events.events.length}`);

    // Calculate summary
    const summary = (timeline as any).calculateSummary(events);
    console.log('\n📈 Summary Metrics:');
    console.log(`  Total: ${summary.totalDurationMs}ms`);
    if (summary.supervisorThinkingMs !== undefined) {
      console.log(`  Supervisor thinking: ${summary.supervisorThinkingMs}ms`);
    }
    if (summary.workerExecutionMs !== undefined) {
      console.log(`  Worker execution: ${summary.workerExecutionMs}ms`);
    }
    if (summary.toolExecutionMs !== undefined) {
      console.log(`  Tool execution: ${summary.toolExecutionMs}ms`);
    }

    // Assertions
    expect(events.totalDurationMs).toBeGreaterThan(0);
    expect(events.totalDurationMs).toBeLessThan(60000); // Max 60s for worker query

    // Should have worker events
    expect(events.phases.worker_spawned).toBeDefined();
    expect(events.phases.worker_started).toBeDefined();

    // Worker execution should be measurable
    if (summary.workerExecutionMs !== undefined) {
      expect(summary.workerExecutionMs).toBeGreaterThan(0);
      expect(summary.workerExecutionMs).toBeLessThan(45000); // Max 45s for worker
    }

    // Export metrics
    const metricsPath = await timeline.exportMetrics('worker-query');
    console.log(`\n💾 Metrics exported to: ${metricsPath}`);
  });

  test('tool execution query - tool latency', async ({ page }) => {
    console.log('\n🧪 TEST: Query with tool execution - tool latency\n');

    await navigateToChatPage(page);

    // Start timeline capture
    const timeline = new TimelineCapture(page);
    await timeline.start();

    // Send query that requires tool execution (e.g., web search)
    await sendMessage(page, 'What is the weather in San Francisco right now?');
    await waitForResponse(page);

    // Stop capture and get events
    const events = await timeline.stop();

    console.log('\n📊 Timeline Summary:');
    console.log(`  Total duration: ${events.totalDurationMs}ms`);
    console.log(`  Correlation ID: ${events.correlationId}`);
    console.log(`  Run ID: ${events.runId}`);
    console.log(`  Event count: ${events.events.length}`);

    // Calculate summary
    const summary = (timeline as any).calculateSummary(events);
    console.log('\n📈 Summary Metrics:');
    console.log(`  Total: ${summary.totalDurationMs}ms`);
    if (summary.supervisorThinkingMs !== undefined) {
      console.log(`  Supervisor thinking: ${summary.supervisorThinkingMs}ms`);
    }
    if (summary.workerExecutionMs !== undefined) {
      console.log(`  Worker execution: ${summary.workerExecutionMs}ms`);
    }
    if (summary.toolExecutionMs !== undefined) {
      console.log(`  Tool execution: ${summary.toolExecutionMs}ms`);
    }

    // Assertions
    expect(events.totalDurationMs).toBeGreaterThan(0);
    expect(events.totalDurationMs).toBeLessThan(90000); // Max 90s for tool query

    // Should have worker and tool events
    expect(events.phases.worker_spawned).toBeDefined();
    expect(events.phases.tool_started).toBeDefined();

    // Tool execution should be measurable
    if (summary.toolExecutionMs !== undefined) {
      expect(summary.toolExecutionMs).toBeGreaterThan(0);
      expect(summary.toolExecutionMs).toBeLessThan(60000); // Max 60s for tools
    }

    // Export metrics
    const metricsPath = await timeline.exportMetrics('tool-query');
    console.log(`\n💾 Metrics exported to: ${metricsPath}`);
  });

  test('compare simple vs worker latency', async ({ page, request }) => {
    console.log('\n🧪 TEST: Compare simple vs worker latency\n');

    await navigateToChatPage(page);

    // Test 1: Simple query
    console.log('\n--- Part 1: Simple query ---');
    const timeline1 = new TimelineCapture(page);
    await timeline1.start();
    await sendMessage(page, 'Reply with just "OK"');
    await waitForResponse(page);
    const events1 = await timeline1.stop();
    const summary1 = (timeline1 as any).calculateSummary(events1);

    console.log(`Simple query duration: ${events1.totalDurationMs}ms`);

    // Clear thread history between tests for isolation
    await request.delete('/api/jarvis/history');
    console.log('🧹 Cleared Jarvis history between queries');

    // Navigate fresh to reset frontend state
    await navigateToChatPage(page);

    // Test 2: Worker query (isolated - no previous conversation context)
    console.log('\n--- Part 2: Worker query ---');
    const timeline2 = new TimelineCapture(page);
    await timeline2.start();
    await sendMessage(page, 'Check the current timestamp');
    await waitForResponse(page);
    const events2 = await timeline2.stop();
    const summary2 = (timeline2 as any).calculateSummary(events2);

    console.log(`Worker query duration: ${events2.totalDurationMs}ms`);

    // Comparison (both queries now have isolated conversation context)
    console.log('\n📊 COMPARISON:');
    console.log(`  Simple: ${events1.totalDurationMs}ms`);
    console.log(`  Worker: ${events2.totalDurationMs}ms`);
    console.log(`  Overhead: ${events2.totalDurationMs - events1.totalDurationMs}ms`);

    // Worker query should take longer
    expect(events2.totalDurationMs).toBeGreaterThan(events1.totalDurationMs);

    // Worker overhead should be reasonable (< 30s)
    const overhead = events2.totalDurationMs - events1.totalDurationMs;
    expect(overhead).toBeLessThan(30000);

    // Export comparison metrics
    await timeline1.exportMetrics('comparison-simple');
    await timeline2.exportMetrics('comparison-worker');

    console.log('\n✅ Comparison complete');
  });
});
