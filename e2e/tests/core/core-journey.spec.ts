/**
 * Core User Journey E2E tests.
 *
 * Keep this file focused on the current surface:
 * - direct Oikos responses
 * - generic tool-card rendering
 * - API event persistence
 */

import { randomUUID } from 'node:crypto';

import { test, expect, type Page } from '../fixtures';
import { postSseAndCollect } from '../helpers/sse';
import { resetDatabase } from '../test-utils';

async function navigateToChatPage(page: Page): Promise<void> {
  await page.goto('/chat');
  await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 15000 });
}

async function waitForEventBusReady(page: Page): Promise<void> {
  await page.waitForFunction(() => {
    const bus = (window as any).__oikos?.eventBus;
    return !!bus && typeof bus.listenerCount === 'function' && bus.listenerCount('oikos:tool_started') > 0;
  }, null, { timeout: 15000 });
}

async function getRunEvents(
  request: import('@playwright/test').APIRequestContext,
  runId: number,
  eventType?: string
): Promise<{ events: Array<{ event_type: string; payload: Record<string, unknown> }>; total: number }> {
  const url = eventType
    ? `/api/oikos/runs/${runId}/events?event_type=${eventType}`
    : `/api/oikos/runs/${runId}/events`;

  const response = await request.get(url);
  expect(response.status()).toBe(200);
  return response.json();
}

test.describe('Core User Journey - Scripted LLM', () => {
  test.setTimeout(120000);

  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test('oikos direct response flow with gpt-scripted model', async ({ request, backendUrl, commisId }) => {
    const events = await postSseAndCollect({
      backendUrl,
      commisId,
      path: '/api/oikos/chat',
      payload: {
        message: 'hello oikos',
        message_id: randomUUID(),
        model: 'gpt-scripted',
        client_correlation_id: 'e2e-core-journey-test',
      },
      stopEvent: 'oikos_complete',
      timeoutMs: 60000,
    });

    const connectedEvent = events.find((e) => e.event === 'connected');
    expect(connectedEvent).toBeTruthy();
    const runId = (connectedEvent?.data as { run_id?: number })?.run_id;
    expect(runId).toBeTruthy();

    const completeEvent = events.find((e) => e.event === 'oikos_complete');
    expect(completeEvent).toBeTruthy();

    const completePayload = (completeEvent?.data as { payload?: { result?: string } })?.payload;
    const result = completePayload?.result || '';
    expect(/^(ok|task completed successfully\.?)$/i.test(result.trim())).toBeTruthy();

    let allEvents = await getRunEvents(request, runId!);
    await expect
      .poll(
        async () => {
          allEvents = await getRunEvents(request, runId!);
          const types = allEvents.events.map((e) => e.event_type);
          return types.includes('oikos_started');
        },
        { timeout: 20000, intervals: [200, 500, 1000, 2000] }
      )
      .toBeTruthy();

    expect(allEvents.events.some((e) => e.event_type === 'oikos_started')).toBeTruthy();
  });

  test('run status indicator is present in DOM', async ({ page }) => {
    await navigateToChatPage(page);

    const statusIndicator = page.locator('[data-testid="run-status"]');
    await expect(statusIndicator).toBeAttached({ timeout: 5000 });
    await expect(statusIndicator).toHaveAttribute('data-run-status', /^(idle|complete)$/, { timeout: 5000 });
  });

  test('generic tool card shows spawn_commis preview', async ({ page }) => {
    await navigateToChatPage(page);
    await waitForEventBusReady(page);

    const runId = 101;
    const toolCallId = 'call-spawn-preview';
    const uniqueToken = `e2e-${randomUUID().slice(0, 8)}`;
    const task = `Check disk space (${uniqueToken})`;

    await page.evaluate(
      ({ runId, toolCallId, task }) => {
        const bus = (window as any).__oikos.eventBus;
        const now = Date.now();

        bus.emit('oikos:started', { runId, task: 'Tool preview test', timestamp: now });
        bus.emit('oikos:tool_started', {
          runId,
          toolName: 'spawn_commis',
          toolCallId,
          argsPreview: task,
          args: { task },
          timestamp: now + 1,
        });
      },
      { runId, toolCallId, task }
    );

    const toolCard = page.locator(`[data-testid="tool-card"][data-tool-call-id="${toolCallId}"]`);
    await expect(toolCard).toBeVisible({ timeout: 5000 });
    await expect(toolCard).toContainText('Start cloud session');
    await expect(toolCard).toContainText(uniqueToken);
  });

  test('tool card expands to show input, result, and raw payload', async ({ page }) => {
    await navigateToChatPage(page);
    await waitForEventBusReady(page);

    const runId = 102;
    const toolCallId = 'call-spawn-details';
    const task = 'Analyze repository state';
    const resultPreview = 'Workspace commis completed successfully.';

    await page.evaluate(
      ({ runId, toolCallId, task, resultPreview }) => {
        const bus = (window as any).__oikos.eventBus;
        const now = Date.now();

        bus.emit('oikos:started', { runId, task: 'Tool details test', timestamp: now });
        bus.emit('oikos:tool_started', {
          runId,
          toolName: 'spawn_commis',
          toolCallId,
          argsPreview: task,
          args: { task, git_repo: 'https://github.com/octocat/Hello-World.git' },
          timestamp: now + 1,
        });
        bus.emit('oikos:tool_completed', {
          runId,
          toolName: 'spawn_commis',
          toolCallId,
          durationMs: 1200,
          resultPreview,
          result: { status: 'success', summary: resultPreview },
          timestamp: now + 2,
        });
      },
      { runId, toolCallId, task, resultPreview }
    );

    const toolCard = page.locator(`[data-testid="tool-card"][data-tool-call-id="${toolCallId}"]`);
    await expect(toolCard).toBeVisible({ timeout: 5000 });
    await toolCard.click();

    await expect(toolCard).toContainText('Input:');
    await expect(toolCard).toContainText(task);
    await expect(toolCard).toContainText('Result:');
    await expect(toolCard).toContainText(resultPreview);

    const rawToggle = toolCard.getByRole('button', { name: 'Show Raw' });
    await rawToggle.click();

    await expect(toolCard).toContainText('"git_repo": "https://github.com/octocat/Hello-World.git"');
    await expect(toolCard).toContainText('"summary": "Workspace commis completed successfully."');
  });

  test('tool card shows failed state', async ({ page }) => {
    await navigateToChatPage(page);
    await waitForEventBusReady(page);

    const runId = 103;
    const toolCallId = 'call-spawn-failed';
    const task = 'Check remote runner health';
    const error = 'Runner offline: cube is not responding';

    await page.evaluate(
      ({ runId, toolCallId, task, error }) => {
        const bus = (window as any).__oikos.eventBus;
        const now = Date.now();

        bus.emit('oikos:started', { runId, task: 'Tool failure test', timestamp: now });
        bus.emit('oikos:tool_started', {
          runId,
          toolName: 'spawn_commis',
          toolCallId,
          argsPreview: task,
          args: { task },
          timestamp: now + 1,
        });
        bus.emit('oikos:tool_failed', {
          runId,
          toolName: 'spawn_commis',
          toolCallId,
          durationMs: 500,
          error,
          errorDetails: { code: 'runner_offline' },
          timestamp: now + 2,
        });
      },
      { runId, toolCallId, task, error }
    );

    const toolCard = page.locator(`[data-testid="tool-card"][data-tool-call-id="${toolCallId}"]`);
    await expect(toolCard).toBeVisible({ timeout: 5000 });
    await toolCard.click();
    await expect(toolCard).toContainText(error);
  });
});

test.describe('Core Journey - API Flow', () => {
  test.setTimeout(60000);

  test('oikos_complete event contains result text', async ({ backendUrl, commisId }) => {
    const events = await postSseAndCollect({
      backendUrl,
      commisId,
      path: '/api/oikos/chat',
      payload: {
        message: 'hello',
        message_id: randomUUID(),
        model: 'gpt-mock',
        client_correlation_id: 'e2e-api-test',
      },
      stopEvent: 'oikos_complete',
      timeoutMs: 30000,
    });

    const connectedEvent = events.find((e) => e.event === 'connected');
    expect(connectedEvent).toBeTruthy();

    const completeEvent = events.find((e) => e.event === 'oikos_complete');
    expect(completeEvent).toBeTruthy();

    const completePayload = (completeEvent?.data as { payload?: { result?: string; status?: string } })?.payload;
    expect(completePayload).toBeTruthy();
    expect(completePayload?.status).toBe('success');
  });

  test('run events endpoint returns events for a run', async ({ request, backendUrl, commisId }) => {
    const events = await postSseAndCollect({
      backendUrl,
      commisId,
      path: '/api/oikos/chat',
      payload: {
        message: 'test message',
        message_id: randomUUID(),
        model: 'gpt-mock',
        client_correlation_id: 'e2e-events-test',
      },
      stopEvent: 'oikos_complete',
      timeoutMs: 30000,
    });

    const connectedEvent = events.find((e) => e.event === 'connected');
    const runId = (connectedEvent?.data as { run_id?: number })?.run_id;
    expect(runId).toBeTruthy();

    const eventsResponse = await request.get(`/api/oikos/runs/${runId}/events`);
    expect(eventsResponse.status()).toBe(200);

    const eventsData = await eventsResponse.json();
    expect(eventsData.run_id).toBe(runId);
    expect(eventsData.events).toBeInstanceOf(Array);
    expect(eventsData.total).toBeGreaterThanOrEqual(0);
  });
});
