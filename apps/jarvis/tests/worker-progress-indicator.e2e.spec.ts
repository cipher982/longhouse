import { test, expect } from '@playwright/test';

/**
 * E2E Tests for Worker Progress Indicator
 *
 * These tests run against the real Jarvis UI, but inject supervisor/worker events
 * directly via `window.__jarvis.eventBus` (DEV only) so they are deterministic and
 * don't depend on LLM/tool timing.
 *
 * The Worker Progress UI only shows when the supervisor delegates to workers.
 * It does NOT show a "thinking" indicator - that's handled by the assistant
 * message bubble's typing state.
 *
 * Test suites:
 * 1. Worker Flow - worker spawn + tool call rendering
 * 2. UI Lifecycle - show/hide behavior
 *
 * CSS Classes:
 * - .worker-progress (main container)
 * - .worker-progress--active (when workers are displayed)
 * - .worker-progress--sticky/.worker-progress--floating (positioning modes)
 * - .supervisor-spinner, .supervisor-label (modal elements)
 * - .supervisor-workers, .supervisor-worker (worker list)
 * - .worker-tool (tool call items)
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
    const el = document.getElementById('worker-progress');
    return Boolean(el) && el.classList.contains('worker-progress');
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
 * SUITE 1: WORKER FLOW TESTS
 *
 * Tests worker spawning, tool calls, and completion rendering.
 * ============================================================================
 */
test.describe('Worker Progress - Worker Flow', () => {
  test.beforeEach(async ({ page }) => {
    await waitForChatReady(page);
  });

  test('shows UI when worker spawns', async ({ page }) => {
    test.setTimeout(60000);

    // supervisor:started alone should NOT show the UI
    await emitEvent(page, 'supervisor:started', { runId: 1, task: 'Test task' });

    // UI should remain hidden
    const container = page.locator('.worker-progress');
    await expect(container).not.toBeVisible({ timeout: 1000 });

    // Worker spawning activates the UI
    await emitEvent(page, 'supervisor:worker_spawned', { jobId: 1, task: 'Check disk space' });

    // Now UI should be visible
    await expect(container).toBeVisible({ timeout: 3000 });

    // Should have active class
    await expect(container).toHaveClass(/worker-progress--active/);

    // Should show investigating spinner and label
    await expect(page.locator('.supervisor-spinner')).toBeVisible();
    await expect(page.locator('.supervisor-label')).toContainText('Investigating');

    // Should show worker list
    await expect(page.locator('.supervisor-workers')).toBeVisible();
    await expect(page.locator('.supervisor-worker')).toBeVisible();
  });

  test('shows tool calls within worker', async ({ page }) => {
    test.setTimeout(60000);

    await emitEvent(page, 'supervisor:started', { runId: 2, task: 'Tool test' });
    await emitEvent(page, 'supervisor:worker_spawned', { jobId: 1, task: 'Run command' });
    await emitEvent(page, 'supervisor:worker_started', { jobId: 1, workerId: 'worker-1' });

    // Wait for UI to be visible
    const container = page.locator('.worker-progress');
    await expect(container).toBeVisible({ timeout: 5000 });

    // Emit tool events
    await emitEvent(page, 'worker:tool_started', {
      workerId: 'worker-1',
      toolName: 'ssh_exec',
      toolCallId: 'tool-1',
      argsPreview: '{"cmd":"df -h"}',
    });

    // Tool should appear
    const toolCall = page.locator('.worker-tool');
    await expect(toolCall.first()).toBeVisible({ timeout: 5000 });
    await expect(toolCall.first().locator('.tool-name')).toContainText('ssh_exec');

    // Complete the tool
    await emitEvent(page, 'worker:tool_completed', {
      workerId: 'worker-1',
      toolName: 'ssh_exec',
      toolCallId: 'tool-1',
      durationMs: 150,
      resultPreview: 'Filesystem...',
    });

    // Tool should show completed state with duration
    await expect(toolCall.first().locator('.tool-duration')).toBeVisible();
  });

  test('shows multiple workers', async ({ page }) => {
    test.setTimeout(60000);

    await emitEvent(page, 'supervisor:started', { runId: 3, task: 'Multi-worker' });
    await emitEvent(page, 'supervisor:worker_spawned', { jobId: 1, task: 'Worker A' });
    await emitEvent(page, 'supervisor:worker_spawned', { jobId: 2, task: 'Worker B' });

    const container = page.locator('.worker-progress');
    await expect(container).toBeVisible({ timeout: 5000 });

    // Should show both workers
    const workers = page.locator('.supervisor-worker');
    await expect(workers).toHaveCount(2, { timeout: 5000 });

    // Should show active count
    const activeCount = page.locator('.supervisor-active-count');
    await expect(activeCount).toBeVisible();
    await expect(activeCount).toContainText(/2 workers? running/);
  });
});


/**
 * ============================================================================
 * SUITE 2: UI LIFECYCLE TESTS
 *
 * Tests show/hide behavior and cleanup.
 * ============================================================================
 */
test.describe('Worker Progress - Lifecycle', () => {
  test.beforeEach(async ({ page }) => {
    await waitForChatReady(page);
  });

  test('hides after workers complete', async ({ page }) => {
    test.setTimeout(60000);

    await emitEvent(page, 'supervisor:started', { runId: 10, task: 'Quick task' });
    await emitEvent(page, 'supervisor:worker_spawned', { jobId: 1, task: 'Worker' });

    const container = page.locator('.worker-progress');
    await expect(container).toBeVisible({ timeout: 5000 });

    // Complete the worker
    await emitEvent(page, 'supervisor:worker_complete', {
      jobId: 1,
      workerId: 'worker-1',
      status: 'success',
      durationMs: 100,
    });

    // Complete the supervisor
    await emitEvent(page, 'supervisor:complete', {
      runId: 10,
      result: 'Done',
      status: 'success',
    });

    // UI should hide (with brief delay to show final state)
    await expect(container).not.toBeVisible({ timeout: 5000 });
  });

  test('stays hidden for quick responses without workers', async ({ page }) => {
    test.setTimeout(30000);

    // supervisor:started + complete without workers should never show UI
    await emitEvent(page, 'supervisor:started', { runId: 20, task: 'Hello' });

    const container = page.locator('.worker-progress');
    await expect(container).not.toBeVisible({ timeout: 500 });

    await emitEvent(page, 'supervisor:complete', {
      runId: 20,
      result: 'Hello there!',
      status: 'success',
    });

    // Should still be hidden
    await expect(container).not.toBeVisible({ timeout: 500 });
  });

  test('no duplicate containers on rapid events', async ({ page }) => {
    test.setTimeout(30000);

    // Fire rapid events
    await emitEvent(page, 'supervisor:started', { runId: 30, task: 'First' });
    await emitEvent(page, 'supervisor:worker_spawned', { jobId: 1, task: 'W1' });
    await emitEvent(page, 'supervisor:complete', { runId: 30, result: 'ok', status: 'success' });
    await emitEvent(page, 'supervisor:started', { runId: 31, task: 'Second' });
    await emitEvent(page, 'supervisor:worker_spawned', { jobId: 2, task: 'W2' });

    // Wait a moment for any duplicate rendering
    await page.waitForTimeout(500);

    // Should only have ONE container
    const containers = page.locator('.worker-progress');
    const count = await containers.count();
    expect(count).toBeLessThanOrEqual(1);
  });

  test('sticky mode: container has position sticky', async ({ page }) => {
    test.setTimeout(30000);

    await emitEvent(page, 'supervisor:started', { runId: 40, task: 'Sticky test' });
    await emitEvent(page, 'supervisor:worker_spawned', { jobId: 1, task: 'Worker' });

    const container = page.locator('.worker-progress');
    await expect(container).toBeVisible({ timeout: 5000 });

    // Check for sticky class
    const hasSticky = await container.evaluate((el) =>
      el.classList.contains('worker-progress--sticky')
    );

    if (hasSticky) {
      const position = await container.evaluate((el) =>
        window.getComputedStyle(el).position
      );
      expect(position).toBe('sticky');
    }
  });
});
