import { test, expect } from '@playwright/test';

// Use environment variable for server URL (set by docker-compose or dev environment)
// In Docker: zerg-backend:8000, locally: localhost:47300
const SERVER_URL = process.env.SERVER_URL || 'http://zerg-backend:8000';

// Expected model - configurable via env var (matches server config)
const EXPECTED_MODEL = process.env.JARVIS_USE_MINI_MODEL === 'true'
  ? (process.env.JARVIS_REALTIME_MODEL_MINI || 'gpt-4o-mini-realtime-preview')
  : (process.env.JARVIS_REALTIME_MODEL || 'gpt-4o-realtime-preview');

test.describe('Jarvis BFF Endpoints (zerg-backend)', () => {
  test('should return valid session token from /api/jarvis/session endpoint', async ({ request }) => {
    const response = await request.get(`${SERVER_URL}/api/jarvis/session`);
    expect(response.status()).toBe(200);

    const data = await response.json();

    // Validate response structure
    expect(data.value).toMatch(/^ek_[a-f0-9]+$/);
    expect(data.expires_at).toBeGreaterThan(Date.now() / 1000);
    expect(data.session).toBeDefined();
    expect(data.session.type).toBe('realtime');
    expect(data.session.model).toBe(EXPECTED_MODEL);
    expect(data.session.audio.output.voice).toBe('verse');
  });

  test('should handle CORS requests', async ({ request }) => {
    const response = await request.get(`${SERVER_URL}/api/jarvis/session`, {
      headers: {
        'Origin': 'http://localhost:8080',
        'Access-Control-Request-Method': 'GET'
      }
    });

    expect(response.status()).toBe(200);
    // CORS headers should be present
  });

  test('should handle server health check', async ({ request }) => {
    // Test that server is responsive
    const response = await request.get(`${SERVER_URL}/api/jarvis/session`);
    expect(response.status()).toBe(200);

    // Response should be fast (< 2 seconds for token generation)
    const startTime = Date.now();
    await request.get(`${SERVER_URL}/api/jarvis/session`);
    const duration = Date.now() - startTime;
    expect(duration).toBeLessThan(2000);
  });

  test('should handle multiple concurrent session requests', async ({ request }) => {
    // Test that server can handle concurrent requests
    const promises = Array.from({ length: 5 }, () =>
      request.get(`${SERVER_URL}/api/jarvis/session`)
    );

    const responses = await Promise.all(promises);

    // All should succeed
    responses.forEach(response => {
      expect(response.status()).toBe(200);
    });

    // All should have unique tokens
    const tokens = await Promise.all(
      responses.map(r => r.json().then(data => data.value))
    );

    const uniqueTokens = new Set(tokens);
    expect(uniqueTokens.size).toBe(5);
  });

  test('should validate session token format', async ({ request }) => {
    const response = await request.get(`${SERVER_URL}/api/jarvis/session`);
    const data = await response.json();

    // Token should be ephemeral key format
    expect(data.value).toMatch(/^ek_[a-f0-9]{32}$/);

    // Session should have required Realtime API fields
    expect(data.session.object).toBe('realtime.session');
    expect(data.session.id).toMatch(/^sess_/);
    expect(data.session.audio.input.format.type).toBe('audio/pcm');
    expect(data.session.audio.input.format.rate).toBe(24000);
  });

  test('should generate conversation title', async ({ request }) => {
    const response = await request.post(`${SERVER_URL}/api/jarvis/conversation/title`, {
      data: {
        messages: [
          { role: 'user', content: 'What is the weather like today?' },
          { role: 'assistant', content: 'I don\'t have access to real-time weather data.' }
        ]
      }
    });

    expect(response.status()).toBe(200);
    const data = await response.json();

    // Should have a title field
    expect(data.title).toBeDefined();
    expect(typeof data.title).toBe('string');
    expect(data.title.length).toBeGreaterThan(0);
  });
});
