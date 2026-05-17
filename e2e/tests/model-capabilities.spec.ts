/**
 * E2E Test: Model Capabilities & Reasoning Selector
 *
 * Tests that the reasoning effort selector follows the active model registry.
 */

import { test, expect } from './fixtures';

const EXPECTED_MODELS = ['deepseek/deepseek-v4-flash', 'deepseek/deepseek-v4-pro'];

test.describe('Model Capabilities & Reasoning Selector', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/chat');

    const modelSelector = page.locator('.model-select');
    await expect(modelSelector).toBeVisible({ timeout: 10000 });
  });

  test('reasoning selector visible for DeepSeek Pro', async ({ page }) => {
    const modelSelector = page.locator('.model-select');
    const reasoningSelector = page.locator('.reasoning-select');

    await modelSelector.selectOption('deepseek/deepseek-v4-pro');

    await expect(reasoningSelector).toBeVisible();
    await expect(reasoningSelector.locator('option[value="none"]')).toBeAttached();
    await expect(reasoningSelector.locator('option[value="low"]')).toBeAttached();
    await expect(reasoningSelector.locator('option[value="medium"]')).toBeAttached();
    await expect(reasoningSelector.locator('option[value="high"]')).toBeAttached();
  });

  test('reasoning selector visible for DeepSeek Flash', async ({ page }) => {
    const modelSelector = page.locator('.model-select');
    const reasoningSelector = page.locator('.reasoning-select');

    await modelSelector.selectOption('deepseek/deepseek-v4-flash');

    await expect(reasoningSelector).toBeVisible();
    await expect(reasoningSelector.locator('option[value="none"]')).toBeAttached();
  });

  test('reasoning effort keeps none when switching between standard models', async ({ page }) => {
    const modelSelector = page.locator('.model-select');
    const reasoningSelector = page.locator('.reasoning-select');

    await modelSelector.selectOption('deepseek/deepseek-v4-pro');
    await reasoningSelector.selectOption('none');
    await expect(reasoningSelector).toHaveValue('none');

    await modelSelector.selectOption('deepseek/deepseek-v4-flash');
    await expect(reasoningSelector).toHaveValue('none');
  });

  test('all configured models are available in selector', async ({ page }) => {
    const modelSelector = page.locator('.model-select');

    for (const modelId of EXPECTED_MODELS) {
      const option = modelSelector.locator(`option[value="${modelId}"]`);
      await expect(option).toBeAttached();
    }
  });
});
