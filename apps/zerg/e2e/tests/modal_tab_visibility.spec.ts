import { test, expect } from './fixtures';

// NB:  This is a *smoke* test – we do not attempt to mock the entire backend.
// The compiled WASM app is expected to be served on http://localhost:8002 by
// `./build.sh --serve` (see README).  CI starts the dev-server in the
// background before invoking `pnpm playwright test`.

test('Agent modal – only one tab-content is visible', async ({ page }) => {
  // 1) Load SPA
  await page.goto('http://localhost:8002');

  // 2) First create an agent so we have something to edit
  await page.click('text="Create Agent"');
  
  // 3) Wait for the agent to appear in the table - should be fast
  await page.waitForSelector('.edit-btn', { timeout: 3000 });

  // 4) Now click the Edit button (✎) to open the modal
  await page.click('[data-testid="edit-agent-1"]');

  // 5) Wait for the modal to become visible using Playwright's visibility check
  await page.waitForSelector('[data-testid="agent-modal"]', { state: 'visible', timeout: 3000 });

  // 6) Navigate to Triggers, then back to Main.
  await page.click('#agent-triggers-tab');
  await page.click('#agent-main-tab');

  // 7) Assert exactly one visible .tab-content inside the modal.
  const visibleSections = await page.$$('#agent-modal .tab-content:not([hidden])');
  expect(visibleSections).toHaveLength(1);
});
