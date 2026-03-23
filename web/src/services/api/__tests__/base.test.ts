import { describe, it, expect, vi } from 'vitest';

// Mock the config module before importing buildUrl
vi.mock('../../../lib/config', () => ({
  config: { apiBaseUrl: '/api' },
}));

import { buildUrl } from '../base';

describe('buildUrl', () => {
  it('prepends /api to a path without prefix', () => {
    expect(buildUrl('/capabilities/llm')).toBe('/api/capabilities/llm');
  });

  it('prepends /api to path without leading slash', () => {
    expect(buildUrl('capabilities/llm')).toBe('/api/capabilities/llm');
  });

  it('strips duplicate /api prefix to prevent double-prefix bug', () => {
    // Passing "/api/foo" should produce "/api/foo", not "/api/api/foo"
    expect(buildUrl('/api/capabilities/llm')).toBe('/api/capabilities/llm');
    expect(buildUrl('/api/llm/providers')).toBe('/api/llm/providers');
    expect(buildUrl('/api/system/capabilities')).toBe('/api/system/capabilities');
  });

  it('handles nested paths correctly', () => {
    expect(buildUrl('/llm/providers/text/test')).toBe('/api/llm/providers/text/test');
  });
});
