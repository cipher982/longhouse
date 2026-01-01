/**
 * E2E Test: Chat Performance Evaluation
 *
 * Profiles REAL user experience latency using:
 * 1. DOM timing - When does the user actually SEE content?
 * 2. Console capture - Frontend TimelineLogger output (?log=timeline)
 * 3. Backend API - Server-side breakdown via /runs/{id}/timeline
 *
 * These tests measure what USERS experience, not internal API timing.
 *
 * Phase 1: Granular Supervisor Response Profiling
 * - Track assistant bubble appearance
 * - Track typing dots visibility
 * - Track first actual token rendering
 * - Track streaming completion
 *
 * Phase 2: Worker Progress UI Profiling
 * - Track worker progress panel appearance
 * - Track individual worker events
 * - Track tool call timing
 */

import { test, expect, type Page } from './fixtures';
import type { APIRequestContext } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

// ESM equivalent of __dirname
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/**
 * Phase 1: Supervisor Response Metrics
 */
interface SupervisorMetrics {
  bubbleAppearedAt: number | null;     // .message.assistant first visible
  typingDotsShownAt: number | null;    // .thinking-dots visible
  firstTokenAt: number | null;         // Actual text content appears
  streamingCompleteAt: number | null;  // Streaming ends, message finalized
  promptTokens: number | null;         // From usage metadata
  completionTokens: number | null;     // From usage metadata
  reasoningTokens: number | null;      // From usage metadata
  totalTokens: number | null;          // From usage metadata (input + output)
  tokensPerSecond: number | null;      // Calculated streaming rate
  assistantCharCount: number | null;   // Visible text length (proxy for output length)
}

/**
 * Phase 2: Worker Progress Metrics
 */
interface WorkerMetrics {
  panelAppearedAt: number | null;      // .worker-progress--active visible
  firstWorkerEventAt: number | null;   // First worker spawned/started
  workerCompleteAt: number | null;     // All workers completed
  workerEvents: WorkerEvent[];         // Individual worker events
}

interface WorkerEvent {
  timestamp: number;
  type: 'spawned' | 'started' | 'tool' | 'complete' | 'failed';
  workerId?: string;
  toolName?: string;
  durationMs?: number;
}

/**
 * Combined Performance Metrics
 */
interface PerformanceMetrics {
  // Basic timing
  sendClickedAt: number;
  timeToFirstToken: number | null;
  timeToComplete: number | null;
  runId: number | null;
  backendTimeline: BackendTimelineMetrics | null;

  // Phase 1: Supervisor metrics
  supervisor: SupervisorMetrics;

  // Phase 2: Worker metrics
  worker: WorkerMetrics;

  // Timeline from console (if available)
  timelineEvents: TimelineEvent[];
}

interface TimelineEvent {
  phase: string;
  offsetMs: number;
  metadata?: string;
}

interface BackendTimelineMetrics {
  runId: number;
  correlationId: string | null;
  summary: {
    totalDurationMs: number;
    supervisorThinkingMs: number | null;
    workerExecutionMs: number | null;
    toolExecutionMs: number | null;
  };
  eventCounts: Record<string, number>;
}

/**
 * Navigate to Jarvis chat with timeline logging enabled
 */
async function navigateToChatWithTimeline(page: Page): Promise<void> {
  // Enable timeline mode for clean performance logging
  await page.goto('/chat?log=timeline');

  // Wait for chat interface
  const chatInput = page.locator('.text-input');
  await expect(chatInput).toBeVisible({ timeout: 15000 });
  console.log('✅ Chat page loaded with timeline mode');
}

function parseUsageTitle(title: string): {
  totalTokens: number | null;
  promptTokens: number | null;
  completionTokens: number | null;
  reasoningTokens: number | null;
} {
  const result: {
    totalTokens: number | null;
    promptTokens: number | null;
    completionTokens: number | null;
    reasoningTokens: number | null;
  } = {
    totalTokens: null,
    promptTokens: null,
    completionTokens: null,
    reasoningTokens: null,
  };

  const parseIntFromLine = (line: string): number | null => {
    const num = line.replace(/[^\d]/g, '');
    return num ? parseInt(num, 10) : null;
  };

  for (const rawLine of title.split('\n')) {
    const line = rawLine.trim();
    if (!line) continue;
    if (line.startsWith('Run tokens')) {
      result.totalTokens = parseIntFromLine(line);
    } else if (line.startsWith('Input tokens')) {
      result.promptTokens = parseIntFromLine(line);
    } else if (line.startsWith('Output tokens')) {
      result.completionTokens = parseIntFromLine(line);
    } else if (line.startsWith('Reasoning tokens')) {
      result.reasoningTokens = parseIntFromLine(line);
    }
  }

  return result;
}

async function fetchBackendTimeline(request: APIRequestContext, runId: number): Promise<BackendTimelineMetrics | null> {
  try {
    const resp = await request.get(`/api/jarvis/runs/${runId}/timeline`);
    if (!resp.ok()) {
      const body = await resp.text().catch(() => '');
      console.log(`⚠️ Backend timeline fetch failed (run_id=${runId}): HTTP ${resp.status()} ${body.slice(0, 200)}`);
      return null;
    }

    const payload = await resp.json() as {
      correlation_id: string | null;
      run_id: number;
      events: Array<{ phase: string }>;
      summary: {
        total_duration_ms: number;
        supervisor_thinking_ms: number | null;
        worker_execution_ms: number | null;
        tool_execution_ms: number | null;
      };
    };

    const eventCounts: Record<string, number> = {};
    for (const event of payload.events) {
      eventCounts[event.phase] = (eventCounts[event.phase] ?? 0) + 1;
    }

    return {
      runId: payload.run_id,
      correlationId: payload.correlation_id,
      summary: {
        totalDurationMs: payload.summary.total_duration_ms,
        supervisorThinkingMs: payload.summary.supervisor_thinking_ms,
        workerExecutionMs: payload.summary.worker_execution_ms,
        toolExecutionMs: payload.summary.tool_execution_ms,
      },
      eventCounts,
    };
  } catch {
    return null;
  }
}

/**
 * Capture granular performance metrics for a chat interaction
 */
async function measureChatPerformance(
  page: Page,
  request: APIRequestContext,
  message: string,
  opts?: { trackWorkerPanel?: boolean }
): Promise<PerformanceMetrics> {
  const metrics: PerformanceMetrics = {
    sendClickedAt: 0,
    timeToFirstToken: null,
    timeToComplete: null,
    runId: null,
    backendTimeline: null,
    supervisor: {
      bubbleAppearedAt: null,
      typingDotsShownAt: null,
      firstTokenAt: null,
      streamingCompleteAt: null,
      promptTokens: null,
      completionTokens: null,
      reasoningTokens: null,
      totalTokens: null,
      tokensPerSecond: null,
      assistantCharCount: null,
    },
    worker: {
      panelAppearedAt: null,
      firstWorkerEventAt: null,
      workerCompleteAt: null,
      workerEvents: [],
    },
    timelineEvents: [],
  };

  // Capture timeline output from console
  const timelineLines: string[] = [];
  page.on('console', msg => {
    const text = msg.text();
    for (const line of text.split('\n')) {
      // Capture timeline logger output
      if (line.includes('[Timeline]') || line.includes('T+')) {
        timelineLines.push(line);
      }
    }
  });

  // Get count of existing assistant messages before sending
  const initialAssistantCount = await page.locator('.message.assistant').count();

  // Fill and send message
  const inputSelector = page.locator('.text-input');
  const sendButton = page.locator('.send-button');

  await inputSelector.fill(message);
  console.log(`📝 Sending: "${message}"`);

  // Record exact send time
  metrics.sendClickedAt = Date.now();
  await sendButton.click();
  console.log('📤 Message sent');

  // Track the new assistant message (the one that appears after our send)
  const newAssistantMessage = page.locator('.message.assistant').nth(initialAssistantCount);

  // Phase 1 Tracking: Supervisor Response Timeline

  // 1. Wait for assistant bubble to appear
  try {
    await expect(newAssistantMessage).toBeVisible({ timeout: 30000 });
    metrics.supervisor.bubbleAppearedAt = Date.now();
    const bubbleTime = metrics.supervisor.bubbleAppearedAt - metrics.sendClickedAt;
    console.log(`💬 Bubble appeared: ${bubbleTime}ms`);
  } catch {
    console.log('⚠️ Assistant message did not appear within timeout');
    return metrics;
  }

  // 2. Check if typing dots are visible
  const typingDots = newAssistantMessage.locator('.thinking-dots');
  try {
    await expect(typingDots).toBeVisible({ timeout: 5000 });
    metrics.supervisor.typingDotsShownAt = Date.now();
    const typingTime = metrics.supervisor.typingDotsShownAt - metrics.sendClickedAt;
    console.log(`⏳ Typing dots shown: ${typingTime}ms`);
  } catch {
    // Not all responses show typing dots; ignore.
  }

  // 3. Wait for actual content to appear (not typing dots)
  // The content is in .message-content, and it's NOT .thinking-dots
  try {
    await page.waitForFunction(
      (index: number) => {
        const messages = document.querySelectorAll('.message.assistant');
        const msg = messages[index];
        if (!msg) return false;
        const content = msg.querySelector('.message-content');
        if (!content) return false;
        const hasTypingDots = !!msg.querySelector('.thinking-dots');
        const text = content.textContent?.trim() || '';
        return text.length > 0 && !hasTypingDots;
      },
      initialAssistantCount,
      { timeout: 45000 }
    );

    metrics.supervisor.firstTokenAt = Date.now();
    metrics.timeToFirstToken = metrics.supervisor.firstTokenAt - metrics.sendClickedAt;
    console.log(`⚡ First token visible: ${metrics.timeToFirstToken}ms`);
  } catch {
    console.log('❌ First token never became visible within timeout');
  }

  // Phase 2 Tracking: Worker Progress UI (optional; keep it cheap so perf tests don’t add their own latency)
  if (opts?.trackWorkerPanel) {
    const workerPanel = page.locator('.worker-progress.worker-progress--active');
    try {
      await workerPanel.waitFor({ state: 'visible', timeout: 3000 });
      metrics.worker.panelAppearedAt = Date.now();
      const panelTime = metrics.worker.panelAppearedAt - metrics.sendClickedAt;
      console.log(`🔧 Worker panel visible: ${panelTime}ms`);

      // Quick snapshot of current worker state
      const workerCount = await page.locator('.supervisor-worker').count();
      if (workerCount > 0) {
        metrics.worker.firstWorkerEventAt = Date.now();
        console.log(`👷 Workers visible: ${workerCount}`);
        metrics.worker.workerEvents.push({
          timestamp: Date.now(),
          type: 'spawned',
        });
      }
    } catch {
      // If panel doesn’t show quickly, don’t block the rest of the perf measurement.
    }
  }

  // 4. Wait for message to reach a terminal status
  try {
    await page.waitForFunction(
      (index: number) => {
        const messages = document.querySelectorAll('.message.assistant');
        const msg = messages[index];
        if (!msg) return false;
        const status = msg.getAttribute('data-message-status') || '';
        return status === 'final' || status === 'error' || status === 'canceled';
      },
      initialAssistantCount,
      { timeout: 75000 }
    );

    metrics.supervisor.streamingCompleteAt = Date.now();
    metrics.timeToComplete = metrics.supervisor.streamingCompleteAt - metrics.sendClickedAt;
    console.log(`✅ Streaming complete: ${metrics.timeToComplete}ms`);
  } catch {
    console.log('❌ Message never reached final/error/canceled within timeout');
  }

  // Extract assistant content length (visible text)
  try {
    const assistantText = (await newAssistantMessage.locator('.message-content').innerText()).trim();
    metrics.supervisor.assistantCharCount = assistantText.length;
  } catch {
    // ignore
  }

  // Extract usage metadata (best-effort)
  try {
    const usageEl = newAssistantMessage.locator('.message-usage-text');
    const title = await usageEl.getAttribute('title');
    if (title) {
      const parsed = parseUsageTitle(title);
      metrics.supervisor.totalTokens = parsed.totalTokens;
      metrics.supervisor.promptTokens = parsed.promptTokens;
      metrics.supervisor.completionTokens = parsed.completionTokens;
      metrics.supervisor.reasoningTokens = parsed.reasoningTokens;
    }
  } catch {
    // ignore
  }

  // Calculate tokens per second if we have the data
  if (metrics.supervisor.firstTokenAt && metrics.supervisor.streamingCompleteAt && metrics.supervisor.completionTokens) {
    const streamDuration = metrics.supervisor.streamingCompleteAt - metrics.supervisor.firstTokenAt;
    if (streamDuration >= 200) {
      metrics.supervisor.tokensPerSecond = (metrics.supervisor.completionTokens / streamDuration) * 1000;
    }
  }

  // Parse timeline events from console output
  const seenTimelineLines = new Set<string>();
  for (const rawLine of timelineLines) {
    const line = rawLine.trimEnd();
    if (seenTimelineLines.has(line)) continue;
    seenTimelineLines.add(line);

    const match = line.match(/T\+(\d+)ms\s+(\w+)\s*(.*)?/);
    if (match) {
      metrics.timelineEvents.push({
        phase: match[2],
        offsetMs: parseInt(match[1]),
        metadata: match[3]?.trim(),
      });

      // Best-effort run id extraction from timeline metadata (avoids relying on /runs list).
      if (metrics.runId === null && match[3]) {
        const runMatch = match[3].match(/run_id=(\d+)/);
        if (runMatch) {
          metrics.runId = parseInt(runMatch[1], 10);
        }
      }
    }
  }

  // Fetch backend timeline summary (best-effort)
  if (metrics.runId) {
    const backendTimeline = await fetchBackendTimeline(request, metrics.runId);
    metrics.backendTimeline = backendTimeline;
    if (backendTimeline) {
      // Collapse into a single synthetic timeline event for easy log reading
      metrics.timelineEvents.push({
        phase: 'backend_timeline',
        offsetMs: backendTimeline.summary.totalDurationMs,
        metadata: `supervisor=${backendTimeline.summary.supervisorThinkingMs ?? 'n/a'}ms worker=${backendTimeline.summary.workerExecutionMs ?? 'n/a'}ms tool=${backendTimeline.summary.toolExecutionMs ?? 'n/a'}ms`,
      });
    }
  }

  return metrics;
}

/**
 * Export metrics to JSON file
 */
function exportMetrics(testName: string, metrics: PerformanceMetrics): string {
  const metricsDir = path.join(__dirname, '..', 'metrics');
  if (!fs.existsSync(metricsDir)) {
    fs.mkdirSync(metricsDir, { recursive: true });
  }

  const filename = `${testName}-${Date.now()}.json`;
  const filepath = path.join(metricsDir, filename);

  fs.writeFileSync(filepath, JSON.stringify({
    testName,
    timestamp: new Date().toISOString(),
    metrics,
  }, null, 2));

  return filepath;
}

/**
 * Print detailed metrics summary
 */
function printMetricsSummary(metrics: PerformanceMetrics): void {
  console.log('\n📊 Performance Summary:');
  console.log('─'.repeat(50));

  // Basic timing
  console.log('⏱️  Basic Timing:');
  console.log(`   Time to First Token: ${metrics.timeToFirstToken}ms`);
  console.log(`   Time to Complete: ${metrics.timeToComplete}ms`);
  console.log(`   Run ID: ${metrics.runId ?? 'n/a'}`);

  // Supervisor breakdown
  console.log('\n🤖 Supervisor Response Breakdown:');
  if (metrics.supervisor.bubbleAppearedAt) {
    const bubbleTime = metrics.supervisor.bubbleAppearedAt - metrics.sendClickedAt;
    console.log(`   Bubble appeared: ${bubbleTime}ms`);
  }
  if (metrics.supervisor.typingDotsShownAt) {
    const typingTime = metrics.supervisor.typingDotsShownAt - metrics.sendClickedAt;
    console.log(`   Typing dots: ${typingTime}ms`);
  }
  if (metrics.supervisor.firstTokenAt) {
    const tokenTime = metrics.supervisor.firstTokenAt - metrics.sendClickedAt;
    console.log(`   First token: ${tokenTime}ms`);
  }
  if (metrics.supervisor.streamingCompleteAt) {
    const completeTime = metrics.supervisor.streamingCompleteAt - metrics.sendClickedAt;
    console.log(`   Streaming complete: ${completeTime}ms`);
  }
  if (metrics.supervisor.assistantCharCount !== null) {
    console.log(`   Visible chars: ${metrics.supervisor.assistantCharCount}`);
  }
  if (metrics.supervisor.completionTokens !== null) {
    console.log(`   Output tokens: ${metrics.supervisor.completionTokens}`);
  }
  if (metrics.supervisor.tokensPerSecond) {
    console.log(`   Tokens/sec: ${metrics.supervisor.tokensPerSecond.toFixed(1)}`);
  }
  if (metrics.backendTimeline) {
    const t = metrics.backendTimeline.summary;
    console.log(`   Backend timeline: total=${t.totalDurationMs}ms supervisor=${t.supervisorThinkingMs ?? 'n/a'}ms worker=${t.workerExecutionMs ?? 'n/a'}ms tool=${t.toolExecutionMs ?? 'n/a'}ms`);
  }

  // Worker breakdown (if any)
  if (metrics.worker.panelAppearedAt) {
    console.log('\n🔧 Worker Progress:');
    const panelTime = metrics.worker.panelAppearedAt - metrics.sendClickedAt;
    console.log(`   Panel appeared: ${panelTime}ms`);
    if (metrics.worker.firstWorkerEventAt) {
      const firstWorker = metrics.worker.firstWorkerEventAt - metrics.sendClickedAt;
      console.log(`   First worker: ${firstWorker}ms`);
    }
    if (metrics.worker.workerCompleteAt) {
      const workerDone = metrics.worker.workerCompleteAt - metrics.sendClickedAt;
      console.log(`   Workers done: ${workerDone}ms`);
    }
    console.log(`   Worker events: ${metrics.worker.workerEvents.length}`);
  }

  // Timeline events
  if (metrics.timelineEvents.length > 0) {
    console.log('\n📈 Timeline Events:');
    for (const event of metrics.timelineEvents) {
      console.log(`   T+${event.offsetMs}ms ${event.phase} ${event.metadata || ''}`);
    }
  }

  console.log('─'.repeat(50));
}

// Extend timeout for performance tests since LLM responses can take time
test.setTimeout(120000);

// Reset DB before each test
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database');
});

test.describe('Chat Performance Evaluation', () => {
  // Parallel by default. Set E2E_PERF_SERIAL=1 for steadier baseline metrics.
  if (process.env.E2E_PERF_SERIAL === '1') {
    test.describe.configure({ mode: 'serial' });
  }

  /**
   * TEST 1: TTFT Baseline
   *
   * Measures: Time from send → first token visible
   * Purpose: Baseline latency for prompt processing (independent of output length)
   *
   * Expected breakdown:
   * - Frontend → Backend: ~50-100ms
   * - Backend processing: ~50-100ms
   * - OpenAI prompt processing (~4600 tokens): ~1000-1500ms
   * - First token network: ~50ms
   * Total expected TTFT: ~1200-1800ms
   */
  test('TTFT baseline - measures prompt processing overhead', async ({ page, request }) => {
    console.log('\n🧪 TEST: TTFT Baseline\n');
    console.log('Purpose: Measure time-to-first-token (prompt processing cost)');
    console.log('This is mostly MODEL time processing ~4600 input tokens.\n');

    await navigateToChatWithTimeline(page);

    const metrics = await measureChatPerformance(
      page,
      request,
      'What is 2+2? Reply with just the number, nothing else.'
    );

    printMetricsSummary(metrics);

    // Key metric: TTFT
    expect(metrics.timeToFirstToken).not.toBeNull();
    console.log(`\n📊 TTFT: ${metrics.timeToFirstToken}ms`);
    console.log(`   Prompt tokens: ${metrics.supervisor.promptTokens ?? 'n/a'}`);

    // Sanity: Should complete quickly (tiny output)
    expect(metrics.timeToComplete).not.toBeNull();
    expect(metrics.timeToComplete!).toBeLessThan(30000);

    // Guardrail: Simple math shouldn't spawn workers
    if (metrics.backendTimeline) {
      expect(metrics.backendTimeline.eventCounts['worker_spawned'] ?? 0).toBe(0);
    }

    exportMetrics('ttft-baseline', metrics);
  });

  /**
   * TEST 2: Streaming Throughput
   *
   * Measures: Tokens per second during streaming
   * Purpose: How fast does content flow once streaming starts?
   *
   * We need a substantial response (~150-200 tokens) to measure this accurately.
   * Calculation: completion_tokens / (streamingComplete - firstToken) * 1000
   *
   * Expected: 20-60 tok/s for GPT-4 class models
   */
  test('streaming throughput - measures tokens/sec during output', async ({ page, request }) => {
    console.log('\n🧪 TEST: Streaming Throughput\n');
    console.log('Purpose: Measure output streaming speed (tokens/second)');
    console.log('Needs ~150+ tokens to be meaningful.\n');

    await navigateToChatWithTimeline(page);

    const metrics = await measureChatPerformance(
      page,
      request,
      'Explain the concept of "technical debt" in software engineering. Write exactly 150 words. Be direct and practical.'
    );

    printMetricsSummary(metrics);

    // Must have meaningful output to measure throughput
    expect(metrics.supervisor.completionTokens).not.toBeNull();
    expect(metrics.supervisor.completionTokens!).toBeGreaterThan(100);

    // Calculate throughput
    const streamDuration = (metrics.supervisor.streamingCompleteAt ?? 0) - (metrics.supervisor.firstTokenAt ?? 0);
    const tokensPerSec = metrics.supervisor.tokensPerSecond;

    console.log(`\n📊 Throughput Analysis:`);
    console.log(`   Completion tokens: ${metrics.supervisor.completionTokens}`);
    console.log(`   Stream duration: ${streamDuration}ms`);
    console.log(`   Tokens/sec: ${tokensPerSec?.toFixed(1) ?? 'n/a'}`);

    // Sanity: Should have reasonable throughput (not absurdly slow)
    if (tokensPerSec !== null) {
      expect(tokensPerSec).toBeGreaterThan(5); // Minimum viable streaming
      console.log(`   Assessment: ${tokensPerSec > 30 ? '✅ Good' : tokensPerSec > 15 ? '⚠️ Acceptable' : '❌ Slow'}`);
    }

    // Guardrail: Explanation shouldn't spawn workers
    if (metrics.backendTimeline) {
      expect(metrics.backendTimeline.eventCounts['worker_spawned'] ?? 0).toBe(0);
    }

    exportMetrics('throughput', metrics);
  });

  /**
   * TEST 3: Worker Overhead
   *
   * Measures: Additional latency when spawning a worker vs direct response
   * Purpose: Quantify the cost of the supervisor → worker delegation
   *
   * Worker path adds:
   * - Tool call decision: ~500-1000ms
   * - Worker spawn + prompt: ~500-1000ms
   * - Worker execution: varies
   * - Result synthesis: ~500-1000ms
   */
  test('worker overhead - measures delegation latency cost', async ({ page, request }) => {
    console.log('\n🧪 TEST: Worker Overhead\n');
    console.log('Purpose: Measure extra latency from supervisor → worker delegation\n');

    // Direct response (no worker)
    console.log('--- Direct response (baseline) ---');
    await navigateToChatWithTimeline(page);
    const directMetrics = await measureChatPerformance(
      page,
      request,
      'What is the capital of France? One word answer.'
    );
    console.log(`Direct: TTFT=${directMetrics.timeToFirstToken}ms, Total=${directMetrics.timeToComplete}ms`);

    // Worker response (triggers spawn_worker)
    console.log('\n--- Worker response (with delegation) ---');
    await navigateToChatWithTimeline(page);
    const workerMetrics = await measureChatPerformance(
      page,
      request,
      'Check the current time on the system.',
      { trackWorkerPanel: true }
    );
    console.log(`Worker: TTFT=${workerMetrics.timeToFirstToken}ms, Total=${workerMetrics.timeToComplete}ms`);

    // Analysis
    const ttftOverhead = (workerMetrics.timeToFirstToken ?? 0) - (directMetrics.timeToFirstToken ?? 0);
    const totalOverhead = (workerMetrics.timeToComplete ?? 0) - (directMetrics.timeToComplete ?? 0);

    console.log(`\n📊 Worker Overhead:`);
    console.log(`   TTFT overhead: ${ttftOverhead}ms`);
    console.log(`   Total overhead: ${totalOverhead}ms`);

    // Worker should have spawned
    if (workerMetrics.backendTimeline) {
      const spawned = workerMetrics.backendTimeline.eventCounts['worker_spawned'] ?? 0;
      console.log(`   Workers spawned: ${spawned}`);
      // Note: We don't assert spawned > 0 because the model might answer directly
    }

    // Direct should NOT have spawned workers
    if (directMetrics.backendTimeline) {
      expect(directMetrics.backendTimeline.eventCounts['worker_spawned'] ?? 0).toBe(0);
    }

    exportMetrics('worker-direct', directMetrics);
    exportMetrics('worker-delegated', workerMetrics);
  });

  /**
   * TEST 4: End-to-End Happy Path
   *
   * Measures: Complete user experience for a typical query
   * Purpose: Validate the full flow works and capture all metrics
   *
   * This is the "golden path" test - a realistic user query that should:
   * - Get a helpful response
   * - Complete in reasonable time
   * - Not spawn workers unnecessarily
   */
  test('e2e happy path - realistic user query', async ({ page, request }) => {
    console.log('\n🧪 TEST: E2E Happy Path\n');
    console.log('Purpose: Validate complete user experience with realistic query\n');

    await navigateToChatWithTimeline(page);

    const metrics = await measureChatPerformance(
      page,
      request,
      'Give me 3 quick tips for writing clean code. Keep it brief - one sentence each.'
    );

    printMetricsSummary(metrics);

    // All phases should complete
    expect(metrics.supervisor.bubbleAppearedAt).not.toBeNull();
    expect(metrics.supervisor.firstTokenAt).not.toBeNull();
    expect(metrics.supervisor.streamingCompleteAt).not.toBeNull();
    expect(metrics.timeToComplete).not.toBeNull();

    // Should have meaningful content
    expect(metrics.supervisor.assistantCharCount).not.toBeNull();
    expect(metrics.supervisor.assistantCharCount!).toBeGreaterThan(50);
    expect(metrics.supervisor.assistantCharCount!).toBeLessThan(2000);

    // Should complete in reasonable time
    expect(metrics.timeToComplete!).toBeLessThan(60000);

    // Shouldn't spawn workers for simple advice
    if (metrics.backendTimeline) {
      expect(metrics.backendTimeline.eventCounts['worker_spawned'] ?? 0).toBe(0);
    }

    console.log('\n✅ Happy path complete');
    exportMetrics('e2e-happy-path', metrics);
  });
});
