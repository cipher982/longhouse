import { test, expect } from './fixtures';
import { createAgentViaAPI, sendMessage, waitForAssistantMessage, waitForUserMessage } from '../test-utils';

// Live prod E2E: real UI + real backend + real LLM

test.describe('Prod Live Chat Smoke', () => {
  test('agent chat returns a response', async ({ page, request }) => {
    test.setTimeout(120_000);

    const agentId = await createAgentViaAPI(request);

    await page.goto(`/agent/${agentId}/thread`);
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 20_000 });

    const message = `Smoke test ${Date.now()}: please acknowledge this message in one short sentence.`;
    await sendMessage(page, message);
    await waitForUserMessage(page, message);
    await waitForAssistantMessage(page);

    const assistant = page.locator('.message.assistant').last();
    const text = (await assistant.textContent()) ?? '';
    expect(text.trim().length).toBeGreaterThan(0);
  });
});
