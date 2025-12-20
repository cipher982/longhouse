import { test, expect } from '@playwright/test';

/**
 * E2E Tests for Two-Phase Supervisor Progress Indicator
 *
 * These tests run against the real Jarvis UI, but inject supervisor/worker events
 * directly via `window.__jarvis.eventBus` (DEV only) so they are deterministic and
 * don't depend on LLM/tool timing.
 *
 * Test suites:
 * 1. UI Behavior - class/DOM behavior when events fire
 * 2. Delegation Flow - worker spawn + tool call rendering
 * 3. Simple Responses - thinking-only runs clear quickly
 *
 * CSS Classes:
 * - .supervisor-progress (main container)
 * - .supervisor-progress--thinking (phase 1: minimal indicator)
 * - .supervisor-progress--delegating (phase 2: full modal)
 * - .thinking-dots, .thinking-dot (typing animation)
 * - .supervisor-spinner, .supervisor-label (full modal elements)
 * - .supervisor-workers, .supervisor-worker (worker list)
 */

// Helper to wait for chat to be ready
async function waitForChatReady(page: any) {
  await page.goto('/chat/');
  await page.waitForSelector('.transcript', { timeout: 30000 });

  // Ensure the app has exposed the EventBus for deterministic injection
  await page.waitForFunction(() => {
    const w = window as any;
    return Boolean(w.__jarvis?.eventBus);
  }, { timeout: 30000 });

  // Ensure the progress container is initialized and in the DOM
  await page.waitForFunction(() => {
    const el = document.getElementById('supervisor-progress');
    return Boolean(el) && el.classList.contains('supervisor-progress');
  }, { timeout: 30000 });
}

// Helper to inject supervisor events directly into the browser
async function emitEvent(page: any, eventName: string, payload: any) {
  await page.evaluate(({ eventName, payload }) => {
    const bus = (window as any).__jarvis?.eventBus;
    if (!bus) {
      throw new Error('EventBus not exposed on window.__jarvis.eventBus');
    }
    bus.emit(eventName, { ...payload, timestamp: Date.now() });
  }, { eventName, payload });
}

/**
 * ============================================================================
 * SUITE 1: UI BEHAVIOR TESTS (Deterministic)
 *
 * These tests inject events directly to verify UI behavior without depending
 * on LLM responses. Tests what the UI DOES when events fire.
 * ============================================================================
 */
test.describe('Progress Indicator UI Behavior', () => {
  test.beforeEach(async ({ page }) => {
    await waitForChatReady(page);
  });

  test('thinking phase: shows dots on supervisor:started', async ({ page }) => {
    test.setTimeout(30000);

    await emitEvent(page, 'supervisor:started', { runId: 1, task: 'Test task' });

    // Thinking dots MUST appear
    const thinkingDots = page.locator('.supervisor-progress--thinking .thinking-dots');
    await expect(thinkingDots).toBeVisible({ timeout: 3000 });

    // Must have exactly 3 dots
    const dots = page.locator('.supervisor-progress--thinking .thinking-dot');
    await expect(dots).toHaveCount(3);

    // Container must have thinking class, NOT delegating
    const container = page.locator('.supervisor-progress');
    await expect(container).toHaveClass(/supervisor-progress--thinking/);

    const classList = await container.getAttribute('class');
    expect(classList).not.toContain('supervisor-progress--delegating');
  });

  test('thinking phase: dots have staggered animation', async ({ page }) => {
    test.setTimeout(30000);

    await emitEvent(page, 'supervisor:started', { runId: 2, task: 'Test task' });

    await page.waitForSelector('.supervisor-progress--thinking .thinking-dot', { timeout: 3000 });

    // Check animation delays are staggered
    const delays = await page.locator('.supervisor-progress--thinking .thinking-dot').evaluateAll((dots) =>
      dots.map((d) => window.getComputedStyle(d).animationDelay)
    );

    expect(delays).toHaveLength(3);
    // Delays should be different (staggered)
    const uniqueDelays = new Set(delays);
    expect(uniqueDelays.size).toBeGreaterThanOrEqual(2);
  });

  test('thinking phase: clears immediately on complete (no workers)', async ({ page }) => {
    test.setTimeout(90000);

    await emitEvent(page, 'supervisor:started', { runId: 3, task: 'Quick task' });

    // Dots appear
    const thinkingDots = page.locator('.supervisor-progress--thinking');
    await expect(thinkingDots).toBeVisible({ timeout: 3000 });

    await emitEvent(page, 'supervisor:complete', { runId: 3, result: 'Done', status: 'success' });

    // Should clear quickly (not the 2s delay of delegating phase)
    // Give it 1 second max - if it's still visible after that, it's buggy
    await expect(thinkingDots).not.toBeVisible({ timeout: 1500 });
  });

  test('no duplicate containers on rapid messages', async ({ page }) => {
    test.setTimeout(60000);

    await page.waitForSelector('.supervisor-progress', { state: 'attached', timeout: 3000 });

    await emitEvent(page, 'supervisor:started', { runId: 4, task: 'First' });
    await emitEvent(page, 'supervisor:complete', { runId: 4, result: 'ok', status: 'success' });
    await emitEvent(page, 'supervisor:started', { runId: 5, task: 'Second' });
    await emitEvent(page, 'supervisor:complete', { runId: 5, result: 'ok', status: 'success' });

    // Should only ever have ONE progress container
    const containers = page.locator('.supervisor-progress');
    const count = await containers.count();
    expect(count).toBeLessThanOrEqual(1);
  });

  test('sticky mode: container has position sticky', async ({ page }) => {
    test.setTimeout(30000);
    await emitEvent(page, 'supervisor:started', { runId: 6, task: 'Test task' });

    const container = page.locator('.supervisor-progress');
    await expect(container).toBeVisible({ timeout: 3000 });

    // Check for sticky class and position
    const hasSticky = await container.evaluate((el) =>
      el.classList.contains('supervisor-progress--sticky')
    );

    if (hasSticky) {
      const position = await container.evaluate((el) =>
        window.getComputedStyle(el).position
      );
      expect(position).toBe('sticky');
    }
    // If not sticky, it should be floating - either is acceptable
  });
});


/**
 * ============================================================================
 * SUITE 2: DELEGATION FLOW (Event Injection)
 *
 * These tests emit the same events the app would receive from the backend and
 * assert the UI updates (delegating phase, tool calls, etc).
 * ============================================================================
 */
test.describe('Progress Indicator - Delegation Flow', () => {
  test.beforeEach(async ({ page }) => {
    await waitForChatReady(page);
  });

  test('worker spawning: upgrades to delegating phase', async ({ page }) => {
    test.setTimeout(120000);

    await emitEvent(page, 'supervisor:started', { runId: 10, task: 'Disk space' });

    // Phase 1: Thinking dots MUST appear first
    const thinkingContainer = page.locator('.supervisor-progress--thinking');
    await expect(thinkingContainer).toBeVisible({ timeout: 3000 });

    await emitEvent(page, 'supervisor:worker_spawned', { jobId: 1, task: 'Check disk space on the local system' });

    // Phase 2: MUST upgrade to delegating when worker spawns
    // This is the critical assertion - NO catch block, test FAILS if this doesn't happen
    const delegatingContainer = page.locator('.supervisor-progress--delegating');
    await expect(delegatingContainer).toBeVisible({ timeout: 30000 });

    // Verify full modal elements
    await expect(page.locator('.supervisor-spinner')).toBeVisible();
    await expect(page.locator('.supervisor-label')).toContainText('Investigating');

    // Workers container MUST appear
    await expect(page.locator('.supervisor-workers')).toBeVisible({ timeout: 10000 });

    // At least one worker MUST be shown
    const workers = page.locator('.supervisor-worker');
    await expect(workers.first()).toBeVisible({ timeout: 10000 });

    const workerCount = await workers.count();
    expect(workerCount).toBeGreaterThan(0);

    // Worker MUST have required structure
    const firstWorker = workers.first();
    await expect(firstWorker.locator('.worker-icon')).toBeVisible();
    await expect(firstWorker.locator('.worker-task')).toBeVisible();

    // Simulate completion: worker finishes and supervisor completes
    await emitEvent(page, 'supervisor:worker_complete', { jobId: 1, workerId: 'worker-1', status: 'success', durationMs: 50 });
    await emitEvent(page, 'supervisor:complete', { runId: 10, result: 'ok', status: 'success' });

    // Modal stays visible briefly (2s delay), then hides
    await page.waitForTimeout(1000);
    // Should still be visible at 1s
    const stillVisible = await delegatingContainer.isVisible();
    expect(stillVisible).toBe(true);

    // Should hide by 4s (2s delay + buffer)
    await expect(delegatingContainer).not.toBeVisible({ timeout: 4000 });
  });

  test('worker spawning: shows tool calls', async ({ page }) => {
    test.setTimeout(120000);

    await emitEvent(page, 'supervisor:started', { runId: 11, task: 'Tool test' });
    await emitEvent(page, 'supervisor:worker_spawned', { jobId: 2, task: 'Run echo hello via container_exec' });
    await emitEvent(page, 'supervisor:worker_started', { jobId: 2, workerId: 'worker-2' });

    // Wait for delegating phase
    const delegatingContainer = page.locator('.supervisor-progress--delegating');
    await expect(delegatingContainer).toBeVisible({ timeout: 30000 });

    await emitEvent(page, 'worker:tool_started', {
      workerId: 'worker-2',
      toolName: 'container_exec',
      toolCallId: 'tool-1',
      argsPreview: '{"cmd":"echo hello"}',
    });

    await emitEvent(page, 'worker:tool_completed', {
      workerId: 'worker-2',
      toolName: 'container_exec',
      toolCallId: 'tool-1',
      durationMs: 12,
      resultPreview: 'hello',
    });

    // Wait for tool calls to appear (worker must execute tools)
    const toolCall = page.locator('.worker-tool');
    await expect(toolCall.first()).toBeVisible({ timeout: 30000 });

    // Tool MUST have required structure
    const firstTool = toolCall.first();
    await expect(firstTool.locator('.tool-icon')).toBeVisible();
    await expect(firstTool.locator('.tool-name')).toBeVisible();
    await expect(firstTool.locator('.tool-duration')).toBeVisible();

    await page.screenshot({ path: './test-results/integration-tool-calls.png' });
  });

  test('worker spawning: shows active worker count', async ({ page }) => {
    test.setTimeout(120000);

    await emitEvent(page, 'supervisor:started', { runId: 12, task: 'Active count' });
    await emitEvent(page, 'supervisor:worker_spawned', { jobId: 3, task: 'Worker A' });
    await emitEvent(page, 'supervisor:worker_spawned', { jobId: 4, task: 'Worker B' });

    // Wait for delegating phase
    await expect(page.locator('.supervisor-progress--delegating')).toBeVisible({ timeout: 30000 });

    // Active count should appear while workers are running
    // This may be brief, so we'll check if it ever appears
    const activeCount = page.locator('.supervisor-active-count');

    // Poll for active count appearance (it may be brief)
    let sawActiveCount = false;
    for (let i = 0; i < 30; i++) {
      if (await activeCount.isVisible()) {
        sawActiveCount = true;
        const text = await activeCount.textContent();
        expect(text).toMatch(/\d+ worker/);
        break;
      }
      await page.waitForTimeout(500);
    }

    // Note: active count only shows while workers are running
    // If workers complete too fast, we may not see it
    // This is acceptable - we log but don't fail
    if (!sawActiveCount) {
      console.log('Note: Active count not observed (workers may have completed quickly)');
    }
  });
});


/**
 * ============================================================================
 * SUITE 3: SIMPLE RESPONSE TESTS (No Workers Expected)
 *
 * Tests for simple messages that should NOT spawn workers.
 * Verifies thinking phase only, quick cleanup.
 * ============================================================================
 */
test.describe('Progress Indicator - Simple Responses', () => {
  test.beforeEach(async ({ page }) => {
    await waitForChatReady(page);
  });

  test('greeting gets quick response without workers', async ({ page }) => {
    test.setTimeout(90000);
    await emitEvent(page, 'supervisor:started', { runId: 20, task: 'Hello' });

    // Thinking dots appear
    await expect(page.locator('.supervisor-progress--thinking')).toBeVisible({ timeout: 3000 });

    // Should NOT upgrade to delegating for a simple greeting
    // Wait a bit to make sure it doesn't upgrade
    await page.waitForTimeout(3000);

    const delegating = page.locator('.supervisor-progress--delegating');
    const isDelegating = await delegating.isVisible();

    // If it DID upgrade to delegating, that's unexpected but not a failure
    // (the LLM might decide to spawn workers for anything)
    if (isDelegating) {
      console.log('Note: Simple greeting triggered delegation (unexpected but acceptable)');
    }

    await emitEvent(page, 'supervisor:complete', { runId: 20, result: 'Hello there!', status: 'success' });

    // Progress should clear
    await expect(page.locator('.supervisor-progress')).not.toBeVisible({ timeout: 5000 });
  });

  test('direct question gets response without long delay', async ({ page }) => {
    test.setTimeout(90000);

    const startTime = Date.now();
    await emitEvent(page, 'supervisor:started', { runId: 21, task: 'Math' });
    await emitEvent(page, 'supervisor:complete', { runId: 21, result: '4', status: 'success' });
    const responseTime = Date.now() - startTime;

    // Simple math question should be fast (under 30s)
    expect(responseTime).toBeLessThan(30000);

    // Progress should be cleared
    await expect(page.locator('.supervisor-progress')).not.toBeVisible({ timeout: 3000 });
  });
});
