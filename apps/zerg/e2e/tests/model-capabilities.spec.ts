/**
 * E2E Test: Model Capabilities & Reasoning Selector
 *
 * Tests that the reasoning effort selector is shown/hidden based on model capabilities:
 * - Models with reasoning=true show the selector
 * - Models with reasoning=false hide the selector
 * - Models without reasoningNone=true don't show "None" option
 */

import { test, expect } from './fixtures';

test.describe('Model Capabilities & Reasoning Selector', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to chat page
    await page.goto('/chat');

    // Wait for chat UI to load
    const modelSelector = page.locator('.model-select');
    await expect(modelSelector).toBeVisible({ timeout: 10000 });
  });

  test('reasoning selector visible for gpt-5.2 (supports reasoning)', async ({ page }) => {
    const modelSelector = page.locator('.model-select');
    const reasoningSelector = page.locator('.reasoning-select');

    // Select gpt-5.2 (supports reasoning with none option)
    await modelSelector.selectOption('gpt-5.2');

    // Reasoning selector should be visible
    await expect(reasoningSelector).toBeVisible();

    // Should have "None" option available (reasoningNone=true)
    const noneOption = reasoningSelector.locator('option[value="none"]');
    await expect(noneOption).toBeAttached();
  });

  test('reasoning selector visible for gpt-5-mini (supports reasoning, no none)', async ({
    page,
  }) => {
    const modelSelector = page.locator('.model-select');
    const reasoningSelector = page.locator('.reasoning-select');

    // Select gpt-5-mini (supports reasoning but NOT none)
    await modelSelector.selectOption('gpt-5-mini');

    // Reasoning selector should be visible
    await expect(reasoningSelector).toBeVisible();

    // Should NOT have "None" option (reasoningNone=false)
    const noneOption = reasoningSelector.locator('option[value="none"]');
    await expect(noneOption).not.toBeAttached();

    // Should have low/medium/high options
    await expect(reasoningSelector.locator('option[value="low"]')).toBeAttached();
    await expect(reasoningSelector.locator('option[value="medium"]')).toBeAttached();
    await expect(reasoningSelector.locator('option[value="high"]')).toBeAttached();
  });

  test('reasoning selector hidden for Llama 4 Maverick (no reasoning support)', async ({
    page,
  }) => {
    const modelSelector = page.locator('.model-select');
    const reasoningSelector = page.locator('.reasoning-select');

    // Select Llama 4 Maverick (no reasoning support)
    await modelSelector.selectOption('meta-llama/llama-4-maverick-17b-128e-instruct');

    // Reasoning selector should be hidden
    await expect(reasoningSelector).not.toBeVisible();
  });

  test('reasoning selector visible for Qwen 3 32B (Groq with reasoning)', async ({ page }) => {
    const modelSelector = page.locator('.model-select');
    const reasoningSelector = page.locator('.reasoning-select');

    // Select Qwen 3 32B (Groq model with reasoning support)
    await modelSelector.selectOption('qwen/qwen3-32b');

    // Reasoning selector should be visible
    await expect(reasoningSelector).toBeVisible();

    // Should have "None" option (reasoningNone=true)
    const noneOption = reasoningSelector.locator('option[value="none"]');
    await expect(noneOption).toBeAttached();
  });

  test('switching models updates reasoning selector visibility', async ({ page }) => {
    const modelSelector = page.locator('.model-select');
    const reasoningSelector = page.locator('.reasoning-select');

    // Start with gpt-5.2 (reasoning visible)
    await modelSelector.selectOption('gpt-5.2');
    await expect(reasoningSelector).toBeVisible();

    // Switch to Llama 4 (reasoning hidden)
    await modelSelector.selectOption('meta-llama/llama-4-maverick-17b-128e-instruct');
    await expect(reasoningSelector).not.toBeVisible();

    // Switch back to gpt-5.2 (reasoning visible again)
    await modelSelector.selectOption('gpt-5.2');
    await expect(reasoningSelector).toBeVisible();
  });

  test('reasoning effort resets when switching to model without none support', async ({
    page,
  }) => {
    const modelSelector = page.locator('.model-select');
    const reasoningSelector = page.locator('.reasoning-select');

    // Start with gpt-5.2 and set reasoning to "none"
    await modelSelector.selectOption('gpt-5.2');
    await reasoningSelector.selectOption('none');

    // Verify "none" is selected
    await expect(reasoningSelector).toHaveValue('none');

    // Switch to gpt-5-mini (doesn't support "none")
    await modelSelector.selectOption('gpt-5-mini');

    // Reasoning should be reset to "low" (first available option)
    await expect(reasoningSelector).toHaveValue('low');
  });

  test('all models are available in selector', async ({ page }) => {
    const modelSelector = page.locator('.model-select');

    // Check all expected models are available
    const expectedModels = [
      'gpt-5.2',
      'gpt-5-mini',
      'gpt-5-nano',
      'qwen/qwen3-32b',
      'meta-llama/llama-4-maverick-17b-128e-instruct',
    ];

    for (const modelId of expectedModels) {
      const option = modelSelector.locator(`option[value="${modelId}"]`);
      await expect(option).toBeAttached();
    }
  });
});
