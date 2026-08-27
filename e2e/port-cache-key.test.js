import { describe, expect, test } from 'bun:test';

import { ciPortCacheKey } from './port-cache-key.js';

describe('CI Playwright port cache identity', () => {
  test('isolates parallel jobs and rerun attempts', () => {
    const base = { CI: 'true', GITHUB_RUN_ID: '42' };

    expect(ciPortCacheKey({ ...base, GITHUB_RUN_ATTEMPT: '1', GITHUB_JOB: 'core' }))
      .toBe('42-1-core');
    expect(ciPortCacheKey({ ...base, GITHUB_RUN_ATTEMPT: '1', GITHUB_JOB: 'a11y' }))
      .toBe('42-1-a11y');
    expect(ciPortCacheKey({ ...base, GITHUB_RUN_ATTEMPT: '2', GITHUB_JOB: 'core' }))
      .toBe('42-2-core');
  });

  test('does not create a CI cache identity locally', () => {
    expect(ciPortCacheKey({})).toBe('');
  });
});
