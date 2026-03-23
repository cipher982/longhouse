/**
 * Skills API Tests - Core Suite
 *
 * These tests verify the skills platform API:
 * - GET /api/skills returns bundled skills
 * - GET /api/skills/prompt returns skill prompt
 * - Skills with tool_dispatch are properly configured
 *
 * CORE SUITE: 0 skipped, 0 flaky, retries: 0
 */

import { test, expect } from '../fixtures';

test.describe('Skills API - Core', () => {
  test('GET /api/skills returns bundled skills', async ({ request }) => {
    const response = await request.get('/api/skills');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('skills');
    expect(Array.isArray(body.skills)).toBe(true);

    // Should have at least the bundled skills
    const skillNames = body.skills.map((s: any) => s.name);
    expect(skillNames).toContain('web-search');
    expect(skillNames).toContain('github');
    expect(skillNames).toContain('slack');
    expect(skillNames).toContain('quick-search');
  });

  test('bundled skills have required fields', async ({ request }) => {
    const response = await request.get('/api/skills');
    expect(response.status()).toBe(200);

    const body = await response.json();

    for (const skill of body.skills) {
      // All skills should have these required fields
      expect(skill).toHaveProperty('name');
      expect(skill).toHaveProperty('description');
      expect(typeof skill.name).toBe('string');
      expect(typeof skill.description).toBe('string');
      expect(skill.name.length).toBeGreaterThan(0);
      expect(skill.description.length).toBeGreaterThan(0);
    }
  });

  test('quick-search skill has tool_dispatch configured', async ({ request }) => {
    const response = await request.get('/api/skills');
    expect(response.status()).toBe(200);

    const body = await response.json();
    const quickSearch = body.skills.find((s: any) => s.name === 'quick-search');

    expect(quickSearch).toBeDefined();
    expect(quickSearch.tool_dispatch).toBe('web_search');
  });

  test('GET /api/skills/prompt returns skill prompt', async ({ request }) => {
    const response = await request.get('/api/skills/prompt');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('prompt');
    expect(typeof body.prompt).toBe('string');

    // Prompt should contain skill information
    expect(body.prompt).toContain('Available Skills');
  });

  test('skills prompt includes bundled skill names', async ({ request }) => {
    const response = await request.get('/api/skills/prompt');
    expect(response.status()).toBe(200);

    const body = await response.json();
    const prompt = body.prompt.toLowerCase();

    // Should mention bundled skills
    expect(prompt).toContain('web-search');
    expect(prompt).toContain('github');
    expect(prompt).toContain('quick-search');
  });
});
