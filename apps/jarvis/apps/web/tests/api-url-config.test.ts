import { describe, it, expect } from 'vitest';
import { getZergApiUrl } from '../lib/config';

/**
 * API URL Configuration Tests
 *
 * These tests ensure the API URL configuration doesn't produce
 * duplicate path prefixes (e.g., /api/api/jarvis/supervisor).
 *
 * Regression test for: https://github.com/user/zerg/issues/XXX
 * The JarvisAPIClient already includes /api/ prefix in its endpoint paths,
 * so the baseURL should NOT include /api.
 */

describe('getZergApiUrl', () => {
  it('should return empty string for same-origin API calls', () => {
    const url = getZergApiUrl();
    expect(url).toBe('');
  });

  it('should NOT return /api (would cause double prefix)', () => {
    const url = getZergApiUrl();
    expect(url).not.toBe('/api');
    expect(url).not.toContain('/api');
  });
});

describe('API URL construction', () => {
  it('should construct correct supervisor URL without double /api', () => {
    const baseURL = getZergApiUrl();
    const supervisorEndpoint = '/api/jarvis/supervisor';
    const fullURL = `${baseURL}${supervisorEndpoint}`;

    expect(fullURL).toBe('/api/jarvis/supervisor');
    expect(fullURL).not.toContain('/api/api');
  });

  it('should construct correct SSE events URL without double /api', () => {
    const baseURL = getZergApiUrl();
    const eventsEndpoint = '/api/jarvis/supervisor/events';
    const fullURL = `${baseURL}${eventsEndpoint}?run_id=123`;

    expect(fullURL).toBe('/api/jarvis/supervisor/events?run_id=123');
    expect(fullURL).not.toContain('/api/api');
  });

  it('should construct correct auth URL without double /api', () => {
    const baseURL = getZergApiUrl();
    const authEndpoint = '/api/jarvis/auth';
    const fullURL = `${baseURL}${authEndpoint}`;

    expect(fullURL).toBe('/api/jarvis/auth');
    expect(fullURL).not.toContain('/api/api');
  });
});
