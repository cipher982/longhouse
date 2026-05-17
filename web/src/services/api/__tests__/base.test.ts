import { describe, it, expect, vi } from 'vitest';

// Mock the config module before importing buildUrl
vi.mock('../../../lib/config', () => ({
  config: { apiBaseUrl: '/api' },
}));

import { buildUrl } from '../base';

describe('buildUrl', () => {
  it('prepends /api to a path without prefix', () => {
    expect(buildUrl('/system/capabilities')).toBe('/api/system/capabilities');
  });

  it('prepends /api to path without leading slash', () => {
    expect(buildUrl('system/capabilities')).toBe('/api/system/capabilities');
  });

  it('strips duplicate /api prefix to prevent double-prefix bug', () => {
    // Passing "/api/foo" should produce "/api/foo", not "/api/api/foo"
    expect(buildUrl('/api/system/capabilities')).toBe('/api/system/capabilities');
    expect(buildUrl('/api/health')).toBe('/api/health');
  });

  it('handles nested paths correctly', () => {
    expect(buildUrl('/sessions/abc/events')).toBe('/api/sessions/abc/events');
  });
});
