/**
 * Skills Platform - Prod Verification
 *
 * Verifies skills API works correctly in production.
 * Part of `make verify-prod` checks.
 */

import { test, expect } from './fixtures';

test.describe('Prod Skills Verification', () => {
  test('skills API returns bundled skills', async ({ request }) => {
    const response = await request.get('/api/skills');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('skills');
    expect(Array.isArray(body.skills)).toBe(true);

    // Should have at least 4 bundled skills
    expect(body.skills.length).toBeGreaterThanOrEqual(4);

    // Verify expected bundled skills exist
    const skillNames = body.skills.map((s: any) => s.name);
    expect(skillNames).toContain('web-search');
    expect(skillNames).toContain('github');
    expect(skillNames).toContain('quick-search');
  });

  test('skill with tool_dispatch exists and is configured', async ({ request }) => {
    const response = await request.get('/api/skills');
    expect(response.status()).toBe(200);

    const body = await response.json();

    // Find a skill with tool_dispatch
    const dispatchSkill = body.skills.find((s: any) => s.tool_dispatch);
    expect(dispatchSkill).toBeDefined();
    expect(dispatchSkill.name).toBe('quick-search');
    expect(dispatchSkill.tool_dispatch).toBe('web_search');
  });

  test('skills prompt endpoint works', async ({ request }) => {
    const response = await request.get('/api/skills/prompt');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('prompt');
    expect(body.prompt.length).toBeGreaterThan(100);
  });
});
