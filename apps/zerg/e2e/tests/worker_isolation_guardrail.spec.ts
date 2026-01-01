/**
 * Worker Isolation Guardrail Test
 *
 * This test verifies that the E2E schema isolation is working correctly.
 * Each Playwright worker gets its own PostgreSQL schema (e2e_worker_0, e2e_worker_1, etc.)
 * and data created in one worker should NOT be visible to other workers.
 *
 * This is a critical safety check for our E2E infrastructure.
 */

import { test as base, expect } from '@playwright/test';

// Create two separate test contexts with different worker IDs
const workerA = base.extend({
  request: async ({ playwright }, use, testInfo) => {
    const backendUrl = `http://127.0.0.1:${process.env.BACKEND_PORT || '8001'}`;
    const request = await playwright.request.newContext({
      baseURL: backendUrl,
      extraHTTPHeaders: {
        'X-Test-Worker': '0', // Force worker A to use schema e2e_worker_0
      },
    });
    await use(request);
    await request.dispose();
  },
});

const workerB = base.extend({
  request: async ({ playwright }, use, testInfo) => {
    const backendUrl = `http://127.0.0.1:${process.env.BACKEND_PORT || '8001'}`;
    const request = await playwright.request.newContext({
      baseURL: backendUrl,
      extraHTTPHeaders: {
        'X-Test-Worker': '1', // Force worker B to use schema e2e_worker_1
      },
    });
    await use(request);
    await request.dispose();
  },
});

workerA('Agent created in worker A is NOT visible in worker B', async ({ request: requestA }) => {
  // Reset databases for both workers
  await requestA.post('/admin/reset-database');

  const requestB = await base.request.newContext({
    baseURL: `http://127.0.0.1:${process.env.BACKEND_PORT || '8001'}`,
    extraHTTPHeaders: {
      'X-Test-Worker': '1',
    },
  });

  await requestB.post('/admin/reset-database');

  // Worker A: Create an agent (backend auto-generates "New Agent" placeholder)
  const createResponse = await requestA.post('/api/agents', {
    data: {
      system_instructions: 'Test agent from worker A',
      task_instructions: 'Test task',
      model: 'gpt-5.2',
    },
  });

  expect(createResponse.ok()).toBeTruthy();
  const createdAgent = await createResponse.json();
  expect(createdAgent.id).toBeDefined();
  expect(createdAgent.name).toBe('New Agent');

  console.log(`✅ Worker A created agent with ID: ${createdAgent.id}`);

  // Worker A: Verify the agent exists in its own schema
  const listAResponse = await requestA.get('/api/agents');
  expect(listAResponse.ok()).toBeTruthy();
  const agentsA = await listAResponse.json();
  expect(agentsA.length).toBe(1);
  expect(agentsA[0].name).toBe('New Agent');

  console.log(`✅ Worker A sees 1 agent in its schema`);

  // Worker B: List agents (should be empty - different schema)
  const listBResponse = await requestB.get('/api/agents');
  expect(listBResponse.ok()).toBeTruthy();
  const agentsB = await listBResponse.json();
  expect(agentsB.length).toBe(0);

  console.log(`✅ Worker B sees 0 agents in its schema (isolation confirmed)`);

  // Worker B: Create its own agent
  const createBResponse = await requestB.post('/api/agents', {
    data: {
      system_instructions: 'Test agent from worker B',
      task_instructions: 'Test task',
      model: 'gpt-5.2',
    },
  });

  expect(createBResponse.ok()).toBeTruthy();
  const createdAgentB = await createBResponse.json();
  expect(createdAgentB.id).toBeDefined();
  expect(createdAgentB.name).toBe('New Agent');

  console.log(`✅ Worker B created agent with ID: ${createdAgentB.id}`);

  // Worker B: Should now see 1 agent (its own)
  const listB2Response = await requestB.get('/api/agents');
  expect(listB2Response.ok()).toBeTruthy();
  const agentsB2 = await listB2Response.json();
  expect(agentsB2.length).toBe(1);
  expect(agentsB2[0].name).toBe('New Agent');

  console.log(`✅ Worker B sees 1 agent (its own)`);

  // Worker A: Should still see only 1 agent (its own)
  const listA2Response = await requestA.get('/api/agents');
  expect(listA2Response.ok()).toBeTruthy();
  const agentsA2 = await listA2Response.json();
  expect(agentsA2.length).toBe(1);
  expect(agentsA2[0].name).toBe('New Agent');

  console.log(`✅ Worker A still sees 1 agent (its own)`);
  console.log(`✅ Schema isolation guardrail PASSED`);

  await requestB.dispose();
});
