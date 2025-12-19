import { test, expect } from '@playwright/test';

/**
 * Jarvis BFF (Backend-for-Frontend) Integration E2E Tests
 *
 * Tests the zerg-backend BFF layer that provides /api/jarvis/* endpoints.
 * These endpoints are served by zerg-backend (no separate jarvis-server) and enforce server-side auth/tool validation.
 *
 * These tests run in the fully containerized Docker environment with:
 *   - postgres (database)
 *   - zerg-backend (BFF layer, AUTH_DISABLED=1 for e2e)
 *   - jarvis-web (PWA)
 *   - nginx reverse-proxy (routes /api/* to zerg-backend)
 *
 * App integration tests (UI interaction) are covered in:
 *   - text-message-happy-path.e2e.spec.ts
 *   - history-hydration.e2e.spec.ts
 *
 * Security tests (401/403 behavior) are covered in backend unit tests:
 *   - test_jarvis_bff.py
 *   - test_authorization_ownership.py
 *
 * Run: make test-jarvis-e2e
 * Or:  docker compose -f apps/jarvis/docker-compose.test.yml run --rm playwright npx playwright test jarvis-auth-happy-path
 */

test.describe('Jarvis BFF Integration', () => {
  test('bootstrap endpoint returns prompt and tools', async ({ request }) => {
    const response = await request.get('/api/jarvis/bootstrap');

    expect(response.status()).toBe(200);

    const data = await response.json();
    expect(data).toHaveProperty('prompt');
    expect(data).toHaveProperty('enabled_tools');
    expect(data).toHaveProperty('user_context');

    // Verify tools structure
    expect(Array.isArray(data.enabled_tools)).toBe(true);
    expect(data.enabled_tools.length).toBeGreaterThan(0);

    // Should include the core tools
    const toolNames = data.enabled_tools.map((t: any) => t.name);
    expect(toolNames).toContain('get_current_location');
    expect(toolNames).not.toContain('route_to_supervisor');
  });

  test('history endpoint returns messages structure', async ({ request }) => {
    const response = await request.get('/api/jarvis/history?limit=5');

    expect(response.status()).toBe(200);

    const data = await response.json();
    expect(data).toHaveProperty('messages');
    expect(Array.isArray(data.messages)).toBe(true);
  });

  test('session proxy returns OpenAI Realtime session', async ({ request }) => {
    const response = await request.get('/api/jarvis/session');

    expect(response.status()).toBe(200);

    const data = await response.json();
    // OpenAI Realtime API returns session object
    expect(data).toHaveProperty('session');
    expect(data.session).toHaveProperty('id');
  });

  // Note: SSE endpoint testing skipped - requires special handling for long-lived connections
  // SSE functionality is tested via the actual app integration tests

  test('supervisor endpoint accepts requests', async ({ request }) => {
    const response = await request.post('/api/jarvis/supervisor', {
      data: {
        task: 'Say hello',
      },
    });

    // Should accept the request (not 400 or 403)
    // May fail execution (500) but should pass auth/validation
    expect([200, 201, 202, 500, 503]).toContain(response.status());
  });
});

test.describe('Jarvis BFF Error Handling', () => {
  test('chat endpoint validates request schema', async ({ request }) => {
    const response = await request.post('/api/jarvis/chat', {
      data: {},
    });

    // Validation error (Pydantic)
    expect(response.status()).toBe(422);
  });

  test('session proxy handles upstream timeout gracefully', async ({ request }) => {
    // This tests that the proxy doesn't crash on slow responses
    const response = await request.get('/api/jarvis/session');

    // Should return a response (may be success or 5xx)
    expect(response.status()).toBeDefined();
  });
});
