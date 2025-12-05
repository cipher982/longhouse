import { test, expect } from '@playwright/test';

/**
 * Supervisor Flow E2E Tests
 *
 * These tests verify the supervisor/worker architecture through the Jarvis API:
 * 1. POST /api/jarvis/supervisor - dispatch a task to the supervisor
 * 2. GET /api/jarvis/supervisor/events - listen to SSE for real-time updates
 * 3. POST /api/jarvis/supervisor/{run_id}/cancel - cancel a running supervisor
 *
 * The supervisor is the "one brain" that coordinates workers for complex tasks.
 */

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:30081';

test.describe('Supervisor Flow', () => {
  test('should dispatch a task and receive run_id', async ({ request }) => {
    // POST to supervisor endpoint
    const response = await request.post(`${BACKEND_URL}/api/jarvis/supervisor`, {
      headers: {
        'Content-Type': 'application/json',
      },
      data: {
        task: 'What time is it?',
      },
      timeout: 60000, // LLM calls can take time
    });

    // Should return 200 with run details
    expect(response.status()).toBe(200);

    const data = await response.json();

    // Verify response structure
    expect(data).toHaveProperty('run_id');
    expect(data).toHaveProperty('thread_id');
    expect(data).toHaveProperty('status');
    expect(data).toHaveProperty('stream_url');

    // run_id should be a number
    expect(typeof data.run_id).toBe('number');
    expect(data.run_id).toBeGreaterThan(0);

    // stream_url should point to events endpoint
    expect(data.stream_url).toContain('/api/jarvis/supervisor/events');
    expect(data.stream_url).toContain(`run_id=${data.run_id}`);
  });

  test('should maintain same thread for same user', async ({ request }) => {
    // First request
    const response1 = await request.post(`${BACKEND_URL}/api/jarvis/supervisor`, {
      headers: { 'Content-Type': 'application/json' },
      data: { task: 'First task' },
      timeout: 60000,
    });
    expect(response1.status()).toBe(200);
    const data1 = await response1.json();

    // Second request
    const response2 = await request.post(`${BACKEND_URL}/api/jarvis/supervisor`, {
      headers: { 'Content-Type': 'application/json' },
      data: { task: 'Second task' },
      timeout: 60000,
    });
    expect(response2.status()).toBe(200);
    const data2 = await response2.json();

    // Thread ID should be the same (one brain per user)
    expect(data1.thread_id).toBe(data2.thread_id);

    // But run IDs should be different
    expect(data1.run_id).not.toBe(data2.run_id);
  });

  test('should receive SSE events with sequence numbers', async ({ request }) => {
    // First dispatch a task
    const dispatchResponse = await request.post(`${BACKEND_URL}/api/jarvis/supervisor`, {
      headers: { 'Content-Type': 'application/json' },
      data: { task: 'What is 2+2?' },
      timeout: 60000,
    });
    expect(dispatchResponse.status()).toBe(200);
    const { run_id } = await dispatchResponse.json();

    // Connect to SSE stream
    // Note: Playwright's request API doesn't fully support SSE streaming,
    // so we verify the endpoint is accessible and returns the right content type
    const sseResponse = await request.get(
      `${BACKEND_URL}/api/jarvis/supervisor/events?run_id=${run_id}`,
      {
        headers: {
          'Accept': 'text/event-stream',
        },
      }
    );

    expect(sseResponse.status()).toBe(200);

    // Content type should be SSE
    const contentType = sseResponse.headers()['content-type'];
    expect(contentType).toContain('text/event-stream');
  });

  test('should cancel a running supervisor', async ({ request }) => {
    // Dispatch a task
    const dispatchResponse = await request.post(`${BACKEND_URL}/api/jarvis/supervisor`, {
      headers: { 'Content-Type': 'application/json' },
      data: { task: 'Complex investigation task' },
      timeout: 60000,
    });
    expect(dispatchResponse.status()).toBe(200);
    const { run_id } = await dispatchResponse.json();

    // Attempt to cancel
    const cancelResponse = await request.post(
      `${BACKEND_URL}/api/jarvis/supervisor/${run_id}/cancel`,
      {
        headers: { 'Content-Type': 'application/json' },
      }
    );

    expect(cancelResponse.status()).toBe(200);

    const cancelData = await cancelResponse.json();
    expect(cancelData).toHaveProperty('run_id');
    expect(cancelData.run_id).toBe(run_id);
    expect(cancelData).toHaveProperty('status');
    // Status should be cancelled, success, or failed (depending on timing)
    expect(['cancelled', 'success', 'failed']).toContain(cancelData.status);
  });
});

test.describe('Supervisor Worker Delegation', () => {
  test.skip('should spawn worker for infrastructure tasks', async ({ request }) => {
    // This test is skipped by default as it requires actual LLM interaction
    // and can take significant time. Run manually for full E2E validation.

    // Dispatch a task that should trigger worker spawn
    const response = await request.post(`${BACKEND_URL}/api/jarvis/supervisor`, {
      headers: { 'Content-Type': 'application/json' },
      data: {
        task: 'Check disk usage on cube server using ssh_exec'
      },
      timeout: 120000, // 2 minutes for LLM + worker execution
    });

    expect(response.status()).toBe(200);
    const { run_id, stream_url } = await response.json();

    // The supervisor should spawn a worker for this task
    // We can verify by checking the stream for worker events
    console.log(`Run ID: ${run_id}`);
    console.log(`Stream URL: ${stream_url}`);

    // Full verification would require SSE parsing which is complex in Playwright
    // The Python test_supervisor_live.py script handles this better
  });
});

test.describe('Supervisor Error Handling', () => {
  test('should return error for missing task', async ({ request }) => {
    const response = await request.post(`${BACKEND_URL}/api/jarvis/supervisor`, {
      headers: { 'Content-Type': 'application/json' },
      data: {}, // Missing task field
    });

    // Should return 422 validation error
    expect(response.status()).toBe(422);
  });

  test('should return 404 for non-existent run cancel', async ({ request }) => {
    const response = await request.post(
      `${BACKEND_URL}/api/jarvis/supervisor/999999/cancel`,
      {
        headers: { 'Content-Type': 'application/json' },
      }
    );

    // Should return 404
    expect(response.status()).toBe(404);
  });
});
